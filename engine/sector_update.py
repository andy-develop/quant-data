#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 行业轮动策略 · 每日更新（与红利低波流水线同仓、同工程规范）

数据链路（纯 HTTP，无 akshare，GitHub Actions 可用）：
  1) 21 行业 × 30 只 ETF 日线：东方财富 push2his（前复权收盘 / 成交额 / 换手率）
  2) 沪深300 全收益 H00300 + 价格 000300：中证指数官网 index-perf（与红利低波同源）
→ 硬校验（有效行业占比 ≥ MIN_VALID_RATIO，不足直接红）→ 统一引擎 sector_engine.py
  （因子 / 回测 / 风控 / 快照，与单测共用同一实现）
→ 生成 sector payload（排行 / 持仓 / 风控 / 回测 / ETF 映射 / 口径披露）
→ 与现有 index.html 中的红利低波 payload 合并注入（carry-forward，互不覆盖）
→ 原始响应与当日快照归档 data/（可复现、可审计）

增量更新（v1.1，避免每次全量重抓）：
  基线 = 最近一次全量周归档 data/sector-week-<date>.json.gz + 其后每日增量
        data/sector-incr-<date>.json.gz（重建为完整 raw，确定性）
  每次抓取只请求 beg=基线末日+1 的增量区间（每 ETF ~几十行，而非全量几千行）：
    - 前复权刻度检测：增量首日与基线末日收盘价跳变 >11% → 期间除权、历史刻度失效 → 该 ETF 全量兜底
    - 缺口检测：增量首日与基线末日间隔 >10 自然日 → 该 ETF 全量兜底
    - 基线陈旧 > FULL_EVERY_DAYS(7) 天或无基线 → 全量重抓（周基线轮换）
    - SECTOR_QUICK=1（CI）：增量请求失败直接沿用基线行（发布不中断），由 workflow 归档回退双保险
  归档治理：incr 保留最近 30 个、week 保留最近 2 个，其余本地删除（git 树体积可控）；
  旧格式 data/sector-raw-<date>.json.gz 作为一次性迁移基线自动兼容。

用法: python3 sector_update.py [--out index.html] [--data-out sector_data.json] [--dry-run]
                     [--raw data/sector-week-....json.gz] [--fetch-only /tmp/raw.json]
"""
import json, os, sys, datetime, gzip, time, glob
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sector_universe as U
import sector_engine as E
import payload_util

_ARGS = sys.argv[1:]
_OUT = "index.html"; _DATA_OUT = "sector_data.json"; _DRY = False; _RAW = None; _FETCH_ONLY = None
while _ARGS:
    a = _ARGS.pop(0)
    if a == "--out" and _ARGS:
        _OUT = _ARGS.pop(0)
    elif a == "--data-out" and _ARGS:
        _DATA_OUT = _ARGS.pop(0)
    elif a == "--raw" and _ARGS:
        _RAW = _ARGS.pop(0)   # 从归档原始数据重跑（跳过抓取），便于本地复现/验证
    elif a == "--fetch-only" and _ARGS:
        _FETCH_ONLY = _ARGS.pop(0)   # 只抓取+校验，写原始 JSON 到指定路径（CI 一次抓取三跑共用）
    elif a == "--dry-run":
        _DRY = True   # 不覆盖仓库产物、不归档、不携带线上段（幂等断言用）
OUT = os.path.join(BASE, _OUT)
DATA_OUT = os.path.join(BASE, _DATA_OUT)
ARCHIVE_DIR = os.path.join(BASE, "data")

FETCH_WORKERS = 3      # ETF 并发抓取（东财限流明显；全局限流器 + 指数退避见 sector_universe）
FETCH_RETRIES = 6      # 单只 ETF 失败重试次数（含退避 2s×(k+1)）
FETCH_RETRIES_QUICK = 2   # SECTOR_QUICK=1（CI）：封锁时快速失败（2 次重试 + 20s 超时），由基线回退兜底
FULL_EVERY_DAYS = 7    # 基线陈旧超过该天数 → 全量重抓（周基线轮换）
KEEP_INCR = 30         # 增量归档保留个数
KEEP_WEEK = 2          # 全量周归档保留个数
KLINE_SCALE_TOL = 0.005   # 前复权重叠日收盘价比阈值（>0.5% 判除权，v1.1 精确复权因子）
MIN_DAILY_AMT = 5e7    # 流动性披露阈值：近 60 日均成交额 <5000 万 → 披露（P1-9，与 README 口径一致）

# ============ 交易日历（P0-1：收盘状态硬校验，与红利低波流水线同源） ============
_CAL = os.path.join(BASE, "trade_calendar.csv")
_TRADE_DAYS = (set(pd.read_csv(_CAL, parse_dates=["trade_date"])["trade_date"].dt.date.tolist())
               if os.path.exists(_CAL) else None)


def bj_now():
    """北京时间（UTC+8）时间戳。dry-run 固定值保证两次生成逐位一致（幂等断言）。"""
    if _DRY:
        return "dry-run"
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


# ============ 增量归档：基线重建 / 合并（纯函数，单测覆盖） ============

def _kl_date(row):
    """klines 行首列日期（YYYY-MM-DD）。"""
    return row.split(",")[0]


def merge_klines(old, new_rows):
    """按日期合并 klines 行（新行覆盖同日旧行），升序返回。"""
    d = {_kl_date(r): r for r in old}
    for r in new_rows:
        d[_kl_date(r)] = r
    return [d[k] for k in sorted(d)]


def kline_gap_days(old, new_rows):
    """增量首日与基线末日间隔（自然日）；无任一侧返回 0。"""
    if not old or not new_rows:
        return 0
    last_old = datetime.date.fromisoformat(_kl_date(old[-1]))
    first_new = datetime.date.fromisoformat(_kl_date(new_rows[0]))
    return (first_new - last_old).days


def kline_overlap_scale(old, new_rows):
    """精确复权因子检测（P1-10）：增量首日须与基线末日重叠（beg=base_day 多取 1 个重叠日）。
    同一交易日（base_day）新旧收盘价比即复权因子：|因子-1| ≥ KLINE_SCALE_TOL → 期间除权、
    旧基线历史刻度整体失效 → 需全量兜底。返回 |ratio-1| 或 None（无重叠日，由调用方按缺口逻辑处理）。"""
    if not old or not new_rows:
        return None
    ov = [r for r in new_rows if _kl_date(r) == _kl_date(old[-1])]
    if not ov:
        return None
    p_old = float(old[-1].split(",")[2])
    p_new = float(ov[0].split(",")[2])
    if p_old <= 0:
        return None
    return abs(p_new / p_old - 1.0)


# ============ 收盘状态硬校验（P0-1：盘中半截 K 线 + 增量不自愈） ============

def last_closed_date(ref=None):
    """最近一个已收盘交易日：参考时刻 ref（datetime 带时区，缺省=当前）当日
    已过收盘时刻（北京 15:30 保守）才算收盘；否则取参考日前一交易日。
    交易日历缺失时降级周末判断。
    v1.1 修复：① 原实现把未收盘的今天直接当收盘日，盘中抓取时活跃 ETF 的
    半截行（amount 达全天 60%+）漏检污染末日面板；
    ② 收盘后重放盘中抓取的 raw，若用当前时刻会误判半截行为完整 bar——
    须以 raw 的 fetched_at 为参考（见 clean_intraday）。"""
    if ref is None:
        ref = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = ref.date()
    closed_today = ref.hour * 60 + ref.minute >= 15 * 60 + 30
    if _TRADE_DAYS is None:
        d = today if closed_today else today - datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
        return d
    cand = sorted(d for d in _TRADE_DAYS if d <= today)
    if cand and cand[-1] == today and not closed_today:
        cand.pop()
    return cand[-1] if cand else today


def clean_intraday(raw):
    """剔除盘中半截 K 线（P0-1）：末日 > 最近已收盘交易日 → 未收盘 bar，剔除。
    收盘参照 = 抓取时刻 fetched_at：盘中抓的 raw（末日=抓取当天未收盘）在收盘后
    重放时，半截行仍按抓取时刻判定剔除（用当前时刻会误判半截行为完整 bar）。
    注：不再做成交额半截检测——以 fetched_at 为参照后，抓取当天（未收盘）的行
    必然 > 最近收盘日被本规则剔除；对历史完整日做 amount 检测会把真实缩量日
    （如 09-10 全天成交仅近 5 日均 51%）误删。
    返回 (raw, removed)；removed 为 [{code,name,date,reason}] 供披露/审计。"""
    lc = last_closed_date()
    ref = None
    fa = raw.get("fetched_at")
    if fa and fa != "dry-run":
        try:
            ref = datetime.datetime.strptime(fa, "%Y-%m-%d %H:%M").replace(
                tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            lc = last_closed_date(ref)
        except ValueError:
            pass
    removed = []
    for code, v in raw.get("etfs", {}).items():
        kl = v.get("klines", [])
        if not kl:
            continue
        last_d = datetime.date.fromisoformat(_kl_date(kl[-1]))
        if last_d > lc:
            removed.append({"code": code, "name": v.get("name", ""), "date": str(last_d), "reason": "未收盘bar"})
            v["klines"] = kl[:-1]
    return raw, removed


def data_quality(raw):
    """数据质量摘要（P1-9）：换手率范围校验（0<turn<100）+ 成交额零值 + 流动性不达标披露。
    返回 warn 级 issues（不阻断发布）。"""
    issues = []
    turn_bad = {}
    amt_zero = {}
    low_liq = []
    for code, v in raw.get("etfs", {}).items():
        kl = v.get("klines", [])
        bad, zero = 0, 0
        for r in kl:
            p = r.split(",")
            if len(p) < 11:
                continue
            try:
                to, amt = float(p[10]), float(p[6])
            except ValueError:
                continue
            if not (0.0 < to < 100.0):
                bad += 1
            if amt <= 0:
                zero += 1
        if bad:
            turn_bad[f"{code} {v.get('name','')}"] = bad
        if zero:
            amt_zero[f"{code} {v.get('name','')}"] = zero
        # 流动性：近 60 个有效成交额均值
        amts = [float(r.split(",")[6]) for r in kl[-60:] if len(r.split(",")) >= 11]
        if len(amts) >= 20 and np.mean(amts) < MIN_DAILY_AMT:
            low_liq.append(f"{code} {v.get('name','')} 日均{np.mean(amts)/1e4:.0f}万")
    if turn_bad:
        items = "；".join(f"{k}×{v}行" for k, v in list(turn_bad.items())[:6])
        issues.append(f"换手率越界(0<turn<100) {sum(turn_bad.values())} 行：{items}")
    if amt_zero:
        items = "；".join(f"{k}×{v}行" for k, v in list(amt_zero.items())[:6])
        issues.append(f"成交额零值 {sum(amt_zero.values())} 行：{items}")
    if low_liq:
        issues.append(f"近60日均成交额<5000万 {len(low_liq)} 只：{'；'.join(low_liq)}")
    return issues


def merge_csi(old, new_rows):
    """按 tradeDate 合并 csi rows（新覆盖旧），升序返回。"""
    d = {r["tradeDate"]: r for r in old}
    for r in new_rows:
        d[r["tradeDate"]] = r
    return [d[k] for k in sorted(d)]


def load_raw(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fdate(name, prefix):
    """从归档文件名解析日期。如 sector-week-20260910.json.gz -> date(2026,9,10)。"""
    return datetime.datetime.strptime(os.path.basename(name).split(prefix)[1].split(".")[0], "%Y%m%d").date()


def rebuild_base():
    """重建最近完整基线：最新周归档 + 其后每日增量（确定性合并）。
    兼容旧格式 data/sector-raw-<date>.json.gz（一次性迁移基线，增量照常）。
    返回 (raw, base_day, mode)；mode ∈ full | incr | migrate | none。"""
    weeks = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")))
    if weeks:
        p = weeks[-1]
        day = _fdate(p, "sector-week-")
        raw = load_raw(p)
        for incr in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-incr-*.json.gz"))):
            iday = _fdate(incr, "sector-incr-")
            if iday <= day:
                continue
            d = load_raw(incr)
            for code, rows in d.get("etfs", {}).items():
                if code not in raw["etfs"]:
                    raw["etfs"][code] = {"industry": "", "name": "", "klines": []}
                old = raw["etfs"][code].get("klines", [])
                raw["etfs"][code]["klines"] = merge_klines(old, rows)
            for c, rows in d.get("csi", {}).items():
                raw["csi"][c] = merge_csi(raw.get("csi", {}).get(c, []), rows)
            day = max(day, iday)
        return raw, day, "incr"
    old = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-raw-*.json.gz")))
    if old:
        return load_raw(old[-1]), _fdate(old[-1], "sector-raw-"), "migrate"
    return None, None, "none"


def _fetch_retries():
    return FETCH_RETRIES_QUICK if os.environ.get("SECTOR_QUICK") else FETCH_RETRIES


def _grab_em(raw, codes, start, incr=None, base_raw=None):
    """并发抓取一批 ETF（增量区间或全量），并入 raw；失败记录 failures。
    incr 非空时同时记录增量行；base_raw 提供旧行做刻度/缺口检测与合并。"""
    quick = os.environ.get("SECTOR_QUICK")
    retries = _fetch_retries()
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futs = {ex.submit(U.fetch_em_kline, code, start=start, retries=retries,
                          timeout=20 if quick else 40): (ind, code, name)
                for ind, code, name in codes}
        for f in as_completed(futs):
            ind, code, name = futs[f]
            old = (base_raw or {}).get("etfs", {}).get(code, {}).get("klines", []) if base_raw else []
            try:
                new_rows = f.result()["klines"]
                if incr is not None and base_raw is not None and old:
                    # v1.1（P1-10）：精确复权因子（重叠日）替代 11% 阈值猜测——分红 0.5~3% 可检出、
                    # 创业板 ±20% 真涨幅不再假阳性；无重叠日时退回缺口检测
                    scale_jump = kline_overlap_scale(old, new_rows)
                    gap = kline_gap_days(old, new_rows)
                    if (scale_jump is not None and scale_jump >= KLINE_SCALE_TOL) or (scale_jump is None and gap > 10):
                        d2 = U.fetch_em_kline(code, start=U.FETCH_START, retries=retries,
                                              timeout=20 if quick else 40)
                        merged = d2["klines"]
                        incr["etfs"][code] = d2["klines"]
                        print(f"      {code} {name} 复权/缺口检测触发，全量兜底（{len(merged)} 行）")
                    else:
                        merged = merge_klines(old, new_rows)
                        incr["etfs"][code] = new_rows
                else:
                    merged = new_rows
                    # v1.1（P2-4）：基线缺该 ETF 时增量行也写归档，次日 rebuild 不再缺历史
                    if incr is not None:
                        incr["etfs"][code] = new_rows
                raw["etfs"][code] = {"industry": ind, "name": name, "klines": merged}
            except Exception as exc:
                if quick and old:
                    raw["etfs"][code] = {"industry": ind, "name": name, "klines": old}
                    raw["failures"].append({"code": code, "name": name, "industry": ind, "error": str(exc)[:120]})
                    print(f"  [快速回退] {code} {name} 增量失败，沿用基线数据：{str(exc)[:90]}")
                else:
                    raw["failures"].append({"code": code, "name": name, "industry": ind, "error": str(exc)[:160]})
                    print(f"  [失败] {code} {name}（{ind}）：{str(exc)[:120]}")


def fetch_all():
    """并发抓取全部 ETF + 沪深300（全收益/价格），返回原始响应 dict。
    东财限流窗口是全局的：首轮失败后冷却 8s 只重抓失败项（最多 2 轮），
    配合 sector_universe 全局限流器 + 指数退避，实测可稳定抓全。"""
    raw = {"fetched_at": bj_now(), "etfs": {}, "csi": {}, "failures": []}

    def grab(codes):
        _grab_em(raw, codes, U.FETCH_START)

    grab(U.ALL_ETFS)
    # CI 场景（SECTOR_QUICK=1）：runner IP 被东财连接级封锁时快速失败，由 workflow 回退到归档
    max_rnd = 1 if os.environ.get("SECTOR_QUICK") else 2
    for rnd in range(max_rnd):
        if not raw["failures"]:
            break
        pending = [(f["industry"], f["code"], f["name"]) for f in raw["failures"]]
        print(f"      冷却 8s 后重试 {len(pending)} 只失败 ETF（第 {rnd + 1} 轮）...")
        time.sleep(8)
        raw["failures"] = []
        grab(pending)
    for c in ("H00300", "000300"):
        raw["csi"][c] = U.fetch_csi_index(c)
    return raw


def fetch_incremental():
    """增量抓取（v1.1）：基线=重建的最近完整归档，只请求 beg=基线末日+1 的增量区间。
    返回 (raw, mode, incr)；incr 为增量行 dict（供归档，full 时为 None）。"""
    base_raw, base_day, mode = rebuild_base()
    if base_raw is None:
        print("[增量] 无基线归档，首次全量抓取")
        return fetch_all(), "full", None
    if (datetime.date.today() - base_day).days > FULL_EVERY_DAYS:
        print(f"[增量] 基线 {base_day} 陈旧 >{FULL_EVERY_DAYS} 天，全量重抓建立新周基线")
        return fetch_all(), "full", None
    print(f"[增量] 基线 {base_day}（mode={mode}），抓 beg={base_day:%Y%m%d} 至今（含重叠日，v1.1 精确复权）")
    quick = os.environ.get("SECTOR_QUICK")
    raw = {"fetched_at": bj_now(), "etfs": {}, "csi": {}, "failures": []}
    incr = {"date": datetime.date.today().strftime("%Y%m%d"), "base": os.path.basename(
        sorted(glob.glob(os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")))[-1]) if glob.glob(
        os.path.join(ARCHIVE_DIR, "sector-week-*.json.gz")) else "legacy",
        "etfs": {}, "csi": {}}
    start = base_day.strftime("%Y%m%d")
    lc = last_closed_date()
    if base_day >= lc:
        # 基线已含最近已收盘交易日（如当日 CI 与本地先后触发，或盘中触发时基线=昨日已最新）：
        # 无增量区间，沿用基线（v1.1：beg=base_day 后原 start>today 判断恒假，改用收盘日比较）
        print(f"[增量] 基线 {base_day} 已含最近收盘日 {lc}，无增量区间，沿用基线数据")
        return base_raw, "incr", incr
    _grab_em(raw, U.ALL_ETFS, start, incr=incr, base_raw=base_raw)
    # 非 quick：失败项冷却一轮重试（保持鲁棒性）
    if not quick and raw["failures"]:
        pending = [(f["industry"], f["code"], f["name"]) for f in raw["failures"]]
        print(f"      冷却 8s 后重试 {len(pending)} 只失败 ETF...")
        time.sleep(8)
        raw["failures"] = []
        _grab_em(raw, pending, start, incr=incr, base_raw=base_raw)
    # 沪深300（中证无前复权问题，纯合并；失败沿用基线）
    for c in ("H00300", "000300"):
        old = base_raw.get("csi", {}).get(c, [])
        try:
            new_rows = U.fetch_csi_index(c, start=start)
            raw["csi"][c] = merge_csi(old, new_rows)
            incr["csi"][c] = new_rows
        except Exception as exc:
            raw["csi"][c] = old
            raw["failures"].append({"code": c, "name": c, "industry": "CSI", "error": str(exc)[:120]})
            print(f"  [快速回退] {c} 增量失败，沿用基线数据：{str(exc)[:90]}")
    return raw, "incr", incr


def archive_incremental(mode, raw, incr):
    """增量/全量归档 + 滚动清理（incr 保留 KEEP_INCR、week 保留 KEEP_WEEK）。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    day = datetime.date.today().strftime("%Y%m%d")
    if mode == "full":
        p = os.path.join(ARCHIVE_DIR, f"sector-week-{day}.json.gz")
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"      全量周归档 {p}")
    elif mode == "incr" and incr is not None:
        p = os.path.join(ARCHIVE_DIR, f"sector-incr-{day}.json.gz")
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(incr, f, ensure_ascii=False)
        print(f"      增量归档 {p}（ETF {len(incr.get('etfs', {}))} 只 / CSI {len(incr.get('csi', {}))}）")
    # 滚动清理
    for pat, keep in (("sector-incr-*.json.gz", KEEP_INCR), ("sector-week-*.json.gz", KEEP_WEEK)):
        for p in sorted(glob.glob(os.path.join(ARCHIVE_DIR, pat)))[:-keep]:
            try:
                os.remove(p)
            except OSError:
                pass


def validate(raw):
    """硬校验：
    1) 有效行业（有 ≥1 只成员 ETF 数据 ≥ MIN_HISTORY 日）占比 < 80% → 失败；
    2) 标的级断言（P2-3）：32 只 ETF 缺失 ≥3 只 → 失败；
    3) 时效性硬校验（P0-1）：ETF 末日晚于最近已收盘交易日 → 失败（clean_intraday 后仍违反说明数据源异常）。
    返回 (ok_inds, ratio, issues)。"""
    issues = []
    ok = 0
    for ind in U.INDUSTRY_LIST:
        codes = [c for c, v in raw["etfs"].items() if v.get("industry") == ind]
        best = max((len(raw["etfs"][c]["klines"]) for c in codes), default=0)
        if best >= U.MIN_HISTORY:
            ok += 1
        else:
            issues.append(f"{ind}({','.join(codes) or '无'}) 仅 {best} 日")
    ratio = ok / len(U.INDUSTRY_LIST)
    # 标的级断言（P2-3）
    missing = [code for _, code, _ in U.ALL_ETFS
               if code not in raw["etfs"] or not raw["etfs"][code].get("klines")]
    if missing:
        issues.append(f"缺失 {len(missing)}/{len(U.ALL_ETFS)} 只 ETF: {','.join(missing[:10])}")
    # 时效性硬校验（P0-1）
    lc = last_closed_date()
    late = [f"{code} {raw['etfs'][code].get('name','')} 末日 {_kl_date(raw['etfs'][code]['klines'][-1])}"
            for code, v in raw["etfs"].items()
            if v.get("klines") and datetime.date.fromisoformat(_kl_date(v["klines"][-1])) > lc]
    if late:
        issues.append(f"{len(late)} 只 ETF 末日晚于最近已收盘交易日 {lc}：{'；'.join(late[:6])}")
    if ratio < U.MIN_VALID_RATIO or len(missing) >= 3 or late:
        parts = []
        if ratio < U.MIN_VALID_RATIO:
            parts.append(f"有效行业 {ok}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）< {U.MIN_VALID_RATIO:.0%}")
        if len(missing) >= 3:
            parts.append(f"ETF 缺失 {len(missing)} 只")
        if late:
            parts.append(f"末日超前 {len(late)} 只")
        raise RuntimeError("硬校验失败：" + "；".join(parts) + "。" + "；".join(issues[:8]))
    return ok, ratio, issues


def thin_series(arr, n):
    step = max(1, len(arr) // n)
    idx = list(range(0, len(arr), step))
    if idx[-1] != len(arr) - 1:
        idx.append(len(arr) - 1)
    # v1.1（P2-10）：强制包含 argmin/argmax——等步长抽样可能错过回撤/峰值极值点，
    # 图上回撤比 metrics.mdd 浅的失真即由此而来
    for m in (np.argmin(arr), np.argmax(arr)):
        m = int(m)
        if m not in idx:
            idx.append(m)
    idx = sorted(idx)
    return [round(float(arr[i]), 6) for i in idx], [str(arr[i]) for i in idx]


def cost_sensitivity(panel, fac):
    """成本敏感性（P1-14）：滑点+佣金费率按 1/2/4/6 倍重跑回测，输出总收益/夏普/回撤/成本占比。
    纯函数（模块级费率参数临时改写后还原），dry-run 幂等。"""
    orig = (E.SLIP_BPS, E.COMM_RATE, E.COMM_MIN)
    out = {}
    try:
        for mult in (1, 2, 4, 6):
            E.SLIP_BPS = orig[0] * mult
            E.COMM_RATE = orig[1] * mult
            E.COMM_MIN = orig[2] * mult
            m = E.backtest(panel, fac)["metrics"]
            out[f"{mult}x"] = {"total": m.get("total"), "sharpe": m.get("sharpe"),
                               "mdd": m.get("mdd"), "cost_pct": m.get("cost_pct"),
                               "n_trades": m.get("n_trades")}
    finally:
        E.SLIP_BPS, E.COMM_RATE, E.COMM_MIN = orig
    return out


def load_sensitivity():
    """读 data/sector-sensitivity.json（P0-4 过拟合体检，由 sector_sensitivity.py 生成）。
    文件缺失/损坏时返回 None（页面隐藏体检卡，不阻断发布）。"""
    p = os.path.join(ARCHIVE_DIR, "sector-sensitivity.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_sector_payload(panel, fac, bt, snap, issues, raw):
    """页面用 sector 数据段：排行 / 持仓 / 风控 / 回测曲线 / 交易 / 年度 / ETF 映射 / 披露。"""
    n = len(bt["dates"])
    s_nav, s_dates = thin_series(bt["nav"], 950)
    c_nav = thin_series(bt["csi_nav"], 950)[0]   # v1.1（P2-11）移除重复调用
    e_nav = thin_series(bt["ew_nav"], 950)[0]
    s_dd = thin_series(bt["dd"], 950)[0]
    c_dd = thin_series(bt["csi_dd"], 950)[0]
    e_dd = thin_series(bt["ew_dd"], 950)[0]
    trades = [{"date": t["date"], "industry": t["industry"], "action": t["action"],
               "w": round(t["w"] * 100, 1), "fill_px": t["fill_px"], "reason": t["reason"],
               "score": t.get("score")}
              for t in bt["trades"]]
    etf_map = {}
    for ind in U.INDUSTRY_LIST:
        etf_map[ind] = [[code, name] for ind2, code, name in U.ALL_ETFS if ind2 == ind]
    m = snap["metrics"] or {}
    return {
        "generated_at": bj_now(),
        "data_date": snap["data_date"],
        "version": "v1.1",
        "start": E.START,
        "universe": snap["universe"],
        "rankings": snap["rankings"],
        "holdings": snap["holdings"],
        "risk": snap["risk"],
        "params": snap["params"],
        "metrics": m,
        "cost_sensitivity": cost_sensitivity(panel, fac),
        "sensitivity": load_sensitivity(),
        "dates": s_dates,
        "strategy_nav": s_nav,
        "csi_nav": c_nav,
        "ew_nav": e_nav,
        "strategy_dd": s_dd,
        "csi_dd": c_dd,
        "ew_dd": e_dd,
        "annual": m.get("annual", {}),
        "trades": trades[-150:],
        "n_trades_total": len(trades),
        "cost_yuan": bt.get("cost_yuan", 0.0),
        "etf_map": etf_map,
        "issues": issues,
        "fetch_failures": raw.get("failures", []),
        "disclosure": [
            "实现口径：行业篮子＝成员 ETF 日收益等权合成；行业价格＝篮子累计净值 ×100；成交额求和、换手率取均值。",
            "信号＝动量 50%（20 日跳过近 5 日 + 60 日 + 120 日，年化 × 各自 R² 趋势质量后等权，截面 z 标准化）"
            "＋波动率 20%（20 日已实现波动率截面前 30% ×0.7）＋拥挤度 20%（60 日均换手 3 年分位 0.5 + 成交额占比 60 日变化 3 年分位 0.5，截面前 20% ×0.85）"
            "＋反转 10%（近 5 日跌幅截面前 10% 行业 +0.15，小截面按名次放宽到至少最差 1 名）。"
            "截面分位仅基于已入池行业（v1.1 修复未入池行业污染截面统计）。",
            "拥挤度 v1 暂缺「份额变化率」第三维：东财无稳定历史份额接口，页面按两维合成并在参数表中如实标注；"
            "后续接份额数据后按方案恢复 1/3 等权。",
            "因子贡献披露（策略层审计 S-2）：宣称权重「动量 50% / 波动 20% / 拥挤 20% / 反转 10%」描述的是因子构成，"
            "与实际经济贡献无对应——消融实测：去拥挤 −2.2pp（噪声量级；crowd_pct 在 34.7% 样本为 NaN、仅 16.4% 被打折）、"
            "去反转 +4.1pp；最终得分方差占比主项 99.6% / 反转 0.3%。因子取舍待重标定后决策，页面仅如实披露。",
            "持仓：月度最后一个交易日决策、T+1 收盘成交，持有 Top-4 行业等权；换仓门槛 +0.15（z 分加法，"
            "v1.1 修复乘法门槛在负分下方向反转）；持仓得分排名前 60% 留仓观察；月中加速：持仓跌出前 50% 且候选 ≥ 持仓最高分 +0.20 时当月额外轮动一次（每月最多一次）。",
            "执行口径：成交价＝执行日行业收盘价 ×(1±滑点 5bp)；佣金＝max(成交额×万1, 5 元/笔)（v1.1 修复小单佣金低估，"
            "单边实际约 9bp）；单月换手 ≤100%，超出按得分保留高分行。",
            "风控：策略近 20 日超额收益（vs 沪深300 全收益）自近 252 日滚动窗口高点回撤 ≥8pp 且超额为负 → 仓位 50% 并暂停开仓，"
            "市场 20 日动量转正后恢复（v1.1 修复 run_max 单调不减支配；触发需连续确认 5 日、熔断至少保持 10 个交易日，防抖）；"
            "单行业自建仓成本回撤 >12% 无条件平仓；沪深300 60 日波动率处过去一年 >80% 分位 → 仓位上限 70%，>90% → 50%（调仓日生效）。",
            "基准：沪深300 全收益 + 行业等权（含夏普/信息比率/同暴露折算，v1.1 补全——策略平均暴露约 63%，"
            "与 100% 暴露基准直接比总收益不公平，请以同暴露折算行对比）；回测 2018 至今，初始 10 万；夏普年化 252 日、无风险利率 0。",
            "标的池 21 行业 × 32 只 ETF（v1.1 修正 30 文案）；为 2026-09 时点人工挑选、以今日视角回溯历史，存在幸存者/前视偏差；"
            "早期年份可选行业少（2018 年仅 4 行业入池，轮动近乎全部持有）。流动性/换手率异常见页底数据质量披露。",
            "信号与回测基于行业篮子净值（成员等权），实盘请按 ETF 映射以相应 ETF 执行，存在跟踪误差。",
            "本页面仅供策略验证与监控参考，不构成投资建议。",
        ],
    }


def archive(raw, payload):
    """原始响应（gzip 压缩，当日不可变输入）+ 当日计算快照，归档 data/。"""
    day = datetime.date.today().strftime("%Y%m%d")
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    raw_path = os.path.join(ARCHIVE_DIR, f"sector-raw-{day}.json.gz")
    with gzip.open(raw_path, "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    snap_path = os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return raw_path, snap_path


def main():
    if _FETCH_ONLY:
        # CI 用：增量抓取+硬校验，原始 JSON 写盘，供 --raw 三跑共用（抓取量 3×→1×，且只抓增量区间）
        print("[fetch-only] 增量抓取 21 行业 × {} ETF + 沪深300（并发 {}）...".format(len(U.ALL_ETFS), FETCH_WORKERS))
        raw, mode, incr = fetch_incremental()
        raw, removed = clean_intraday(raw)   # v1.1（P0-1）收盘清洗：剔除盘中半截 bar
        dq = data_quality(raw)               # v1.1（P1-9）换手率/流动性质量摘要
        etfs_ok = len(raw["etfs"])
        print(f"      ETF 有效 {etfs_ok}/{len(U.ALL_ETFS)}；失败 {len(raw['failures'])}；"
              f"CSI 全收益/价格 {len(raw['csi'].get('H00300', []))}/{len(raw['csi'].get('000300', []))} 条")
        if removed:
            print(f"      [收盘清洗] 剔除 {len(removed)} 条半截/未收盘 bar：")
            for r in removed[:10]:
                print(f"        {r['code']} {r['name']} {r['date']} {r['reason']}")
        ok_inds, ratio, issues = validate(raw)
        issues = dq + issues
        print(f"      有效行业 {ok_inds}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）")
        for w in issues:
            print("  [警告]", w)
        with open(_FETCH_ONLY, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"      原始数据已写 {_FETCH_ONLY}")
        archive_incremental(mode, raw, incr)
        return
    if _RAW:
        print(f"[1/4] 使用归档原始数据重跑（{_RAW}，跳过抓取）...")
        raw = load_raw(_RAW)
        raw, removed = clean_intraday(raw)
        dq = data_quality(raw)
    else:
        print("[1/4] 增量抓取 21 行业 × {} ETF + 沪深300（并发 {}）...".format(len(U.ALL_ETFS), FETCH_WORKERS))
        raw, mode, incr = fetch_incremental()
        # v1.1（修复隐式归档缺失）：本地直跑也写增量/全量归档——原注释声称"已在抓取阶段完成"
        # 但 fetch_incremental 从不调用 archive_incremental，次日 rebuild_base 会缺今天增量
        archive_incremental(mode, raw, incr)
        raw, removed = clean_intraday(raw)
        dq = data_quality(raw)
    etfs_ok = len(raw["etfs"])
    print(f"      ETF 成功 {etfs_ok}/{len(U.ALL_ETFS)}；失败 {len(raw['failures'])}；"
          f"CSI 全收益/价格 {len(raw['csi'].get('H00300', []))}/{len(raw['csi'].get('000300', []))} 条")
    if removed:
        print(f"      [收盘清洗] 剔除 {len(removed)} 条半截/未收盘 bar：")
        for r in removed[:10]:
            print(f"        {r['code']} {r['name']} {r['date']} {r['reason']}")
    print("[2/4] 硬校验（有效行业占比 + 标的齐备 + 时效性）...")
    ok_inds, ratio, issues = validate(raw)
    issues = dq + issues
    print(f"      有效行业 {ok_inds}/{len(U.INDUSTRY_LIST)}（{ratio:.0%}）")
    for w in issues:
        print("  [警告]", w)
    print("[3/4] 统一引擎：因子 + 回测 + 风控 + 快照...")
    panel = E.build_panel(raw)
    fac = E.compute_factors(panel)
    bt = E.backtest(panel, fac)
    snap = E.snapshot(panel, fac, bt)
    m = snap["metrics"] or {}
    print(f"      覆盖 {len(panel['inds'])} 行业 / {panel['meta']['n_dates']} 交易日，最新 {snap['data_date']}")
    print(f"      回测 {m.get('total', 0)*100:+.1f}% / 夏普 {m.get('sharpe', 0):.2f} / "
          f"回撤 {m.get('mdd', 0)*100:.1f}% / {m.get('n_trades', 0)} 笔 / 年换手 {m.get('avg_annual_turn', 0):.2f}")
    payload = build_sector_payload(panel, fac, bt, snap, issues, raw)
    print("[4/4] 合并注入 index.html（携带红利低波段）...")
    tpl = open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    prev_html = None
    if os.path.exists(OUT) and not _DRY:
        with open(OUT, encoding="utf-8") as f:
            prev_html = f.read()
    prev = payload_util.extract(prev_html) if prev_html else None
    merged = {}
    if prev:
        for k in ("snapshot", "backtest", "hs300"):
            if prev.get(k) is not None:
                merged[k] = prev[k]
    merged["sector"] = payload
    html = payload_util.inject(tpl, merged)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"      sector_data.json 已生成 -> {DATA_OUT}")
    if not _DRY:
        if _RAW:
            # 三跑共用同一 raw：增量/全量归档由 fetch-only 阶段负责，这里只写当日快照
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            day = datetime.date.today().strftime("%Y%m%d")
            with open(os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            print(f"      快照归档 data/sector-snapshot-{day}.json")
        else:
            # 本地直接跑（非 --raw）：增量/全量归档已在抓取阶段完成（fetch_incremental 返回前），再补快照
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            day = datetime.date.today().strftime("%Y%m%d")
            with open(os.path.join(ARCHIVE_DIR, f"sector-snapshot-{day}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            print(f"      快照归档 data/sector-snapshot-{day}.json")


if __name__ == "__main__":
    main()
