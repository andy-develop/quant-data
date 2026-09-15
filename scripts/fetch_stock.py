#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票日K 增量更新（全 A 股，腾讯双通道，规避 WAF 风控）。

通道 A — qt.gtimg.cn 批量快照（全市场约 90 个请求）：取【最后完整交易日】当日完整收盘
         bar → raw 增量。日常增量主通道，请求量小，远离 WAF 阈值。
通道 B — fqkline 逐股（raw 历史补段 / hfq 全部）。hfq 无批量接口只能逐股；全局限速
         ~2.5 QPS + 连续失败退避，防封 IP。
         ⚠️ WAF 按【主机】轮换封禁（实测：某时段 ifzq.gtimg.cn 封 / web.ifzq.gtimg.cn 通，
         数小时后反转）→ 主机池故障转移：请求按存活顺序尝试，被封主机自动降级并 10 分钟后
         重试。
         首选 proxy.finance.qq.com（腾讯官方代理域名，实测 30+ 连发不触发 WAF，hfq 数值与
         ifzq 通道逐位一致）；该通道对个别股票（如 sh688981）缺 hfqday 时自动降级到其余主机。
         注意 max 参数：>800 会被腾讯截断到 640 根（不足 3 年），必须用 800。

- 只更新 meta/stocks.parquet 中 status=1（正常上市）的股票；退市股保留历史不再更新。
- 增量起点 = 现有序列（分片+增量）max(date)+1；无记录默认回看 3 年。
- 抓取截止 = last_complete_day()（15:30 前取上一交易日；12:00 门户 / 16:35 镜像均安全）。
- 输出 data/kline/stock/{raw,hfq}_incr_YYYYMMDD.parquet（每日小文件，控制 git 增量）；
  每周一由 housekeeping --compact 并入年份分片并清理。

用法:
  python3 scripts/fetch_stock.py              # 日常增量（raw 批量 + hfq 逐股）
  python3 scripts/fetch_stock.py --days 5     # 强制回看 N 天（补漏/修复，走 fqkline）
  python3 scripts/fetch_stock.py --backfill   # 全量 3 年（首次或分片缺失，走 fqkline）
"""
import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from fetch_index import last_complete_day  # noqa: E402

UA = {"User-Agent": C.UA, "Referer": "https://gu.qq.com/"}
RETRY = 2              # 单请求失败重试次数
TIMEOUT = 10
BACKFILL_DAYS = 365 * 3
FQ_WORKERS = 2         # fqkline 逐股并发
MIN_INTERVAL = 0.4     # 全局最小请求间隔(s) → ≤9000 请求/小时，远低于 WAF 触发量
WAF_ALERT = 8          # 连续失败达此值 → 视为被风控，退避 60s
WAF_MAX_ROUNDS = 3     # 退避轮数上限：持续被风控则中止，避免空转烧 CI 时间
BATCH_SIZE = 60        # qt.gtimg.cn 单请求最多拼接股票数
FQ_HOSTS = ["proxy.finance.qq.com", "ifzq.gtimg.cn", "web.ifzq.gtimg.cn"]
# fqkline/kline 主机池（proxy 首选：腾讯官方代理域名，WAF 免疫；ifzq/web.ifzq 被 WAF 轮换封禁）
FQ_MAX = 800           # 腾讯 fqkline max 上限：>800 截断到 640 根（不足 3 年），必须 ≤800
HOST_UNBLOCK_S = 600   # 被封主机 10 分钟后自动解封重试

# 全局限速 + 连续失败计数
_rate_lock = threading.Lock()
_last_req = 0.0
_fail_lock = threading.Lock()
_consec_fail = 0
_backoff_rounds = 0

# 主机级 WAF 封禁跟踪
_host_lock = threading.Lock()
_blocked_hosts = {}  # host -> unblock_ts


def _host_blocked(host: str) -> bool:
    return _blocked_hosts.get(host, 0) > time.time()


def _mark_host_blocked(host: str) -> None:
    with _host_lock:
        _blocked_hosts[host] = time.time() + HOST_UNBLOCK_S


def _mark_host_ok(host: str) -> None:
    with _host_lock:
        _blocked_hosts.pop(host, None)


def _pick_hosts() -> list:
    """候选主机：未封在前（轮转），被封 10 分钟到期后自动回归。"""
    with _host_lock:
        now = time.time()
        alive = [h for h in FQ_HOSTS if _blocked_hosts.get(h, 0) <= now]
        dead = [h for h in FQ_HOSTS if _blocked_hosts.get(h, 0) > now]
    return alive + dead


_tl = threading.local()


def _pace() -> None:
    """全局请求节流（跨线程串行化，保证任意时刻 QPS 受控）。"""
    global _last_req
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()


def _waf_backoff() -> None:
    global _consec_fail, _backoff_rounds
    with _fail_lock:
        _consec_fail += 1
        if _consec_fail >= WAF_ALERT:
            _consec_fail = 0
            _backoff_rounds += 1
            if _backoff_rounds > WAF_MAX_ROUNDS:
                raise SystemExit(
                    f"[fetch_stock] 连续被风控 {WAF_MAX_ROUNDS} 轮退避仍失败，中止以避免空转。"
                    " 可稍后重试或检查腾讯接口可用性。")
            print(f"  ... 连续失败 {WAF_ALERT} 次，疑似被风控，退避 60s（第 {_backoff_rounds} 轮）")
            time.sleep(60)


def _reset_fail() -> None:
    global _consec_fail
    with _fail_lock:
        _consec_fail = 0


def _session() -> requests.Session:
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update(UA)
        _tl.s = s
    return s


def _get_json(url: str, host: str = "", backoff: bool = True, retries: int = RETRY):
    """GET JSON；限速 + WAF 识别 + 退避重试。失败返回 None。

    backoff=False（降级探测）：WAF/网络失败只标记该主机被封，不计入全局退避
    （避免 empty 股票降级探测被封主机时误触发退避中止）。
    retries：重试次数；降级探测用 0（单次即可，空数据不值得重试）。"""
    for k in range(retries + 1):
        _pace()
        try:
            r = _session().get(url, timeout=TIMEOUT)
            if r.status_code in (403, 429) or "501page" in r.text[:300]:
                if host:
                    _mark_host_blocked(host)
                raise RuntimeError("waf")
            r.raise_for_status()
            _reset_fail()
            return r.json()
        except Exception:
            if backoff:
                _waf_backoff()
            if k < retries:
                time.sleep(1 + k * 2)
    return None


def _get_text(url: str, host: str = "", backoff: bool = True, retries: int = RETRY):
    """GET 文本（GBK）；限速 + WAF 识别 + 退避重试。失败返回 None。"""
    for k in range(retries + 1):
        _pace()
        try:
            r = _session().get(url, timeout=TIMEOUT)
            if r.status_code in (403, 429) or "501page" in r.text[:300]:
                if host:
                    _mark_host_blocked(host)
                raise RuntimeError("waf")
            r.raise_for_status()
            r.encoding = "gbk"
            _reset_fail()
            return r.text
        except Exception:
            if backoff:
                _waf_backoff()
            if k < retries:
                time.sleep(1 + k * 2)
    return None


def _fqkline_url(host: str, sym: str, start: str, end: str, hfq: bool) -> str:
    """fqkline(kline) URL；host 走主机池故障转移。

    proxy.finance.qq.com 是腾讯代理域名，路径带 /ifzqgtimg 前缀。"""
    path = "/ifzqgtimg/appstock/app" if host == "proxy.finance.qq.com" else "/appstock/app"
    if hfq:
        return (f"https://{host}{path}/fqkline/get"
                f"?param={sym},day,{start},{end},{FQ_MAX},hfq")
    return (f"https://{host}{path}/kline/kline"
            f"?param={sym},day,{start},{end},{FQ_MAX},")


def fetch_fqkline(sym: str, start: str, end: str, hfq: bool) -> pd.DataFrame:
    """抓一只股票 [start,end] 日K；hfq=True 后复权。失败/空 → 空表。

    腾讯行格式: [date, open, close, high, low, volume, ...]（raw 无成交额，amount 补 0）。
    首选 proxy（腾讯代理域名，WAF 免疫）；若其无对应类型数据（个别股票缺 hfqday）
    降级探测其余主机 —— 降级请求 backoff=False，避免误触发全局退避。"""
    for i, host in enumerate(_pick_hosts()):
        # 首主机（proxy）失败算 WAF 退避；降级主机单次探测、不计退避
        j = _get_json(_fqkline_url(host, sym, start, end, hfq), host,
                      backoff=(i == 0), retries=RETRY if i == 0 else 0)
        if not j:
            continue
        _mark_host_ok(host)
        d = (j.get("data") or {}).get(sym) or {}
        rows = d.get("hfqday" if hfq else "day") or []
        if not rows:
            # 该主机无对应类型数据（腾讯侧缺该区间数据/数据源差异），降级尝试下一主机
            continue
        recs = []
        for r in rows:
            try:
                recs.append({"date": pd.Timestamp(r[0]),
                             "open": float(r[1]), "close": float(r[2]),
                             "high": float(r[3]), "low": float(r[4]),
                             "volume": float(r[5])})
            except (ValueError, TypeError, IndexError):
                continue
        return pd.DataFrame(recs)
    return pd.DataFrame()


def batch_snapshot(syms: list) -> dict:
    """qt.gtimg.cn 批量快照 -> {sym: {open,high,low,close,volume,amount=0}}。

    仅保留【当日有成交】(volume>0 且 price>0) 的股票 —— 停牌/盘前/休市均无当日 bar。
    amount 置 0 以与历史 raw 列契约一致（腾讯不复权 K线本就无成交额）。
    qt 被封（WAF）时返回空 dict，由 main 降级走 fqkline raw。"""
    qt_host = "qt.gtimg.cn"
    out = {}
    for i in range(0, len(syms), BATCH_SIZE):
        batch = syms[i:i + BATCH_SIZE]
        txt = _get_text(f"https://{qt_host}/q=" + ",".join(batch), qt_host)
        if not txt:
            continue
        for line in txt.split(";"):
            line = line.strip()  # 响应行带 \n 前缀，strip 后才能拿到干净 sym
            if not line or "=" not in line:
                continue
            sym = line.split("=", 1)[0].replace("v_", "")
            parts = line.split("=", 1)[1].strip('"').split("~")
            if len(parts) < 38:
                continue
            try:
                price, vol = float(parts[3]), float(parts[6])
                if vol <= 0 or price <= 0:
                    continue  # 停牌/无成交：无当日 bar
                out[sym] = {"open": float(parts[5]), "high": float(parts[33]),
                            "low": float(parts[34]), "close": price,
                            "volume": vol, "amount": 0.0}
            except (ValueError, IndexError):
                continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="全量回看 3 年（忽略现有数据）")
    ap.add_argument("--days", type=int, default=0, help="强制回看 N 天（补漏/修复，>0 时忽略现有数据）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只股票（沙箱测试/分片重试）")
    args = ap.parse_args()
    C.ensure_dirs()

    meta = C.read_df(f"{C.META}/stocks.parquet")
    if meta.empty:
        raise SystemExit("[fetch_stock] 无 data/meta/stocks.parquet（先跑 migrate.py）")
    alive = meta.loc[meta["status"].astype(str) == "1", "code"].tolist()
    if args.limit:
        alive = alive[:args.limit]
    print(f"[fetch_stock] 股票 {len(alive):,} 只（status=1）")

    end_s = last_complete_day()
    end = pd.Timestamp(end_s)
    # 现有每股票 max(date)（分片+增量已合并；空表 get → NaN → 回看默认 3 年）
    raw_max = C.stock_read("raw").groupby("code")["date"].max()
    hfq_max = C.stock_read("hfq").groupby("code")["date"].max()
    lookback = end - pd.Timedelta(days=BACKFILL_DAYS)

    def start_for(max_map: pd.Series, code: str):
        if args.backfill or args.days:
            back = args.days if args.days else BACKFILL_DAYS
            return end - pd.Timedelta(days=back)
        m = max_map.get(code)
        if m is None or pd.isna(m):
            return lookback
        return m + pd.Timedelta(days=1)

    # 请求归类：raw 只缺最后完整日 → 批量；其余（多日/无记录/强制）→ fqkline
    batch_codes, fq_jobs = [], []
    for code in alive:
        sym = C.tx_to_symbol(code)
        sr = start_for(raw_max, code)
        if sr <= end:
            if args.backfill or args.days or sr < end:
                fq_jobs.append((code, sym, sr.strftime("%Y-%m-%d"), end_s, False))
            else:
                batch_codes.append(code)
        hf = start_for(hfq_max, code)
        if hf <= end:
            fq_jobs.append((code, sym, hf.strftime("%Y-%m-%d"), end_s, True))
    print(f"[fetch_stock] end={end_s} 批量raw {len(batch_codes):,} 只 / "
          f"fqkline {len(fq_jobs):,} 个请求")

    # ---- 通道 A：批量快照 → raw 当日增量 ----
    batch_rows = []
    if batch_codes:
        snap = batch_snapshot([C.tx_to_symbol(c) for c in batch_codes])
        if snap:
            for code in batch_codes:
                q = snap.get(C.tx_to_symbol(code))
                if not q:
                    continue
                batch_rows.append({"code": code, "date": end, "open": q["open"],
                                   "close": q["close"], "high": q["high"], "low": q["low"],
                                   "volume": q["volume"], "amount": 0.0})
            print(f"[fetch_stock] 批量快照: {len(batch_rows):,}/{len(batch_codes):,} 只取到 {end_s} bar")
        else:
            # qt 被封降级：raw 单日缺口改走 fqkline raw（kline 端点双主机可用）
            fq_jobs += [(code, C.tx_to_symbol(code), end_s, end_s, False) for code in batch_codes]
            print(f"[fetch_stock] qt 批量通道被风控，{len(batch_codes):,} 只降级走 fqkline raw")

    # ---- 通道 B：fqkline（raw 补段 + hfq 全部） ----
    t0 = time.time()
    fq_new_raw, new_hfq, fail = [], [], 0
    if fq_jobs:
        with ThreadPoolExecutor(max_workers=FQ_WORKERS) as ex:
            futs = {ex.submit(fetch_fqkline, sym, s, e, h): (code, h)
                    for code, sym, s, e, h in fq_jobs}
            for i, f in enumerate(as_completed(futs), 1):
                code, hfq = futs[f]
                try:
                    df = f.result()
                except Exception:
                    df = pd.DataFrame()
                if df.empty:
                    fail += 1
                else:
                    df.insert(0, "code", code)
                    if hfq:
                        new_hfq.append(df)
                    else:
                        fq_new_raw.append(df)
                if i % 1000 == 0:
                    print(f"  ... {i}/{len(fq_jobs)} ({time.time() - t0:.0f}s)")
    dt = time.time() - t0

    # ---- 合并写入增量 ----
    new_raw = pd.DataFrame()
    parts = [p for p in [pd.DataFrame(batch_rows), *fq_new_raw] if len(p)]
    if parts:
        new_raw = pd.concat(parts, ignore_index=True)
        if "amount" not in new_raw.columns:
            new_raw["amount"] = 0.0  # 腾讯不复权接口无成交额，补齐列契约
    new_hfq = pd.concat(new_hfq, ignore_index=True) if new_hfq else pd.DataFrame()
    for prefix, df in (("raw", new_raw), ("hfq", new_hfq)):
        if df.empty:
            continue
        df = df.drop_duplicates(["code", "date"])
        if prefix == "raw":
            df = df[["code", "date", "open", "close", "high", "low", "volume", "amount"]]
        else:
            df = df[["code", "date", "open", "high", "low", "close"]]
        C.stock_incr_write(df, prefix)
        print(f"[fetch_stock] {prefix} 新增 {len(df):,} 行 "
              f"{df['date'].min().date()} -> {df['date'].max().date()}")

    total_jobs = len(batch_codes) + len(fq_jobs)
    print(f"[fetch_stock] 完成: 成功 {total_jobs - fail}/{total_jobs}，失败 {fail}，耗时 {dt:.0f}s")
    C.manifest_add({"event": "fetch_stock", "at": C.bj_now(),
                    "batch": len(batch_codes), "fq": len(fq_jobs),
                    "ok": total_jobs - fail, "fail": fail,
                    "sec": int(dt), "end": end_s})


if __name__ == "__main__":
    main()
