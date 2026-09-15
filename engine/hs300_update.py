#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 择时策略（v8.0）· 每日自动更新
抓取 H00300(全收益)/000300(价格) 行情 -> 统一回测引擎(backtest/engine.py) 计算信号与净值
-> 生成 hs300 数据段 -> 与红利低波/行业轮动 carry-forward 合并注入 index.html

口径（v8.0，四态仓位机与红利低波 v7.7 同构，仅 3 参数按沪深300 高波动重标定）：
  信号用价格指数 000300 计算；收益用全收益 H00300；T+1 收盘成交 + 单边滑点 5bp；
  杠杆部分(125/150%)按年化 7% 按交易日计息；费用单边 max(万1, 5元)。
  重标定参数（用户定稿 v8.0，v8.1 按策略层审计实测修正 1 项）：
    X_UP 20→15   （300 波动大，20% 门槛来不及顶部离场）
    Y_DOWN 14→20 → 回 14（策略层审计 H-4 实测：沪深300 63 日跌幅全样本最差仅 −21.6%、2018 年最差 −17.6%，
                     20% 阈值全样本仅触发 3 天/2018 年 0 天，参数被数据禁用；14 恢复该成分作用且略优）
    HOLD_DAYS 60→120（深熊别在坑里强平割肉；审计 H-4：影响 ≤8.4pp，非关键参数，保留定稿值）
  其余结构件原样保留：J/RSI 阈值、布林、杠杆上限 150%、T+1、滑点 5bp、融资 7%、
  双门（年线 MA250 + 估值剪刀差分位）、REBUY_DAYS=90、2-of-4 超卖共振、三维超买极值。

用法: python3 hs300_update.py [--out index.html] [--data-out hs300_data.json] [--dry-run]
"""
import json, os, sys, time, datetime, glob
import urllib.request
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import payload_util
from update import fetch_index, validate_data, trade_day_info, build_snapshot, build_backtest_payload
# 统一回测引擎：优先仓库内 backtest/（同级目录），无则回退上级 backtest/
_ENGINE_DIR = os.path.join(BASE, "backtest")
if not os.path.isdir(_ENGINE_DIR):
    _ENGINE_DIR = os.path.join(os.path.dirname(BASE), "backtest")
sys.path.insert(0, _ENGINE_DIR)
import engine as E

_ARGS = sys.argv[1:]
_OUT = "index.html"; _DATA_OUT = "hs300_data.json"; _DRY = False
while _ARGS:
    a = _ARGS.pop(0)
    if a == "--out" and _ARGS:
        _OUT = _ARGS.pop(0)
    elif a == "--data-out" and _ARGS:
        _DATA_OUT = _ARGS.pop(0)
    elif a == "--dry-run":
        _DRY = True
OUT = os.path.join(BASE, _OUT)
DATA_OUT = os.path.join(BASE, _DATA_OUT)

# ---- 沪深300 变体参数（v8.0 定稿：仅 3 参数重标定，其余与红利低波完全一致）----
P = E.make_params(X_UP=15.0, Y_DOWN=14.0, HOLD_DAYS=120)   # v8.1：Y_DOWN 20→14（审计 H-4 实测 20 被禁用）
START = E.START                 # 与红利低波同回测窗口 2016-09-08（可比）
FETCH_START = "20130719"        # 与红利低波同左边界锚定（H00300/000300 该日起有数据）
ARCHIVE_DIR = os.path.join(BASE, "data")
FULL_EVERY_DAYS = 7
KEEP_INCR = 30
KEEP_WEEK = 2


def _hl_archive_date(name, code):
    return datetime.datetime.strptime(
        os.path.basename(name).split(f"{code}-")[1].split(".")[0].split("-")[-1], "%Y%m%d").date()


def rebuild_hl():
    """重建最近完整基线（H00300/000300）：周归档全量 + 其后每日增量合并（确定性）。"""
    out = {}
    base_day = None
    for code in ("H00300", "000300"):
        weeks = sorted(glob.glob(os.path.join(ARCHIVE_DIR, f"{code}-week-*.json")))
        rows, day = [], None
        if weeks:
            d = json.load(open(weeks[-1], encoding="utf-8"))
            rows = d["rows"]
            day = _hl_archive_date(weeks[-1], code)
            for incr in sorted(glob.glob(os.path.join(ARCHIVE_DIR, f"{code}-incr-*.json"))):
                iday = _hl_archive_date(incr, code)
                if iday <= day:
                    continue
                d2 = json.load(open(incr, encoding="utf-8"))
                dmap = {r["tradeDate"]: r for r in rows}
                for r in d2["rows"]:
                    dmap[r["tradeDate"]] = r
                rows = [dmap[k] for k in sorted(dmap)]
                day = max(day, iday)
        else:
            # 旧全量格式 H00300-YYYYMMDD.json（一次性迁移基线）
            for p in sorted(glob.glob(os.path.join(ARCHIVE_DIR, f"{code}-????????.json")), reverse=True):
                try:
                    d = json.load(open(p, encoding="utf-8"))
                except Exception:
                    continue
                if d.get("indexCode") == code and d.get("rows"):
                    rows = d["rows"]
                    day = _hl_archive_date(p, code)
                    break
        if not rows:
            return None, None
        out[code] = rows
        base_day = day if base_day is None else min(base_day, day)
    return out, base_day


def load_prices(archive=True):
    """在线抓取 H00300(全收益) 与 000300(价格)：增量模式（基线+增量区间合并，拼接安全）；
    无基线或基线陈旧 → 全量。原始响应按类型归档 data/（周全量 / 日增量，供复现核对）。
    返回 (df, raw, paths)。"""
    today = datetime.date.today()
    end = today.strftime("%Y%m%d")
    base, base_day = rebuild_hl()
    mode = "full"
    incr_rows = None
    if base is None:
        print("[增量] 无基线归档，全量抓取（首次）")
        tr_rows = fetch_index("H00300", FETCH_START, end)
        px_rows = fetch_index("000300", FETCH_START, end)
    elif (today - base_day).days > FULL_EVERY_DAYS:
        print(f"[增量] 基线 {base_day} 陈旧 >{FULL_EVERY_DAYS} 天，全量重抓建立新周基线")
        tr_rows = fetch_index("H00300", FETCH_START, end)
        px_rows = fetch_index("000300", FETCH_START, end)
    else:
        mode = "incr"
        start = (base_day + datetime.timedelta(days=1)).strftime("%Y%m%d")
        if start > end:
            print(f"[增量] 基线 {base_day} 已是最新，无增量区间，沿用基线")
            tr_rows, px_rows = base["H00300"], base["000300"]
            incr_rows = {"H00300": [], "000300": []}
        else:
            print(f"[增量] 基线 {base_day}，只抓 {start} 至今的增量区间")
            tr_new = fetch_index("H00300", start, end)
            px_new = fetch_index("000300", start, end)
            tr = {r["tradeDate"]: r for r in base["H00300"]}
            for r in tr_new:
                tr[r["tradeDate"]] = r
            px = {r["tradeDate"]: r for r in base["000300"]}
            for r in px_new:
                px[r["tradeDate"]] = r
            tr_rows = [tr[k] for k in sorted(tr)]
            px_rows = [px[k] for k in sorted(px)]
            incr_rows = {"H00300": tr_new, "000300": px_new}
    tr = {r["tradeDate"]: r["close"] for r in tr_rows}
    px = {r["tradeDate"]: r["close"] for r in px_rows}
    dates = sorted(set(tr) & set(px))
    df = pd.DataFrame({"date": pd.to_datetime(dates), "close": [tr[d] for d in dates], "px": [px[d] for d in dates]})
    df = df.sort_values("date").reset_index(drop=True)
    # 注意：不截断到 START——保留 START 前的指标 warm-up（与红利低波 v7.6 一致）
    if archive:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        day = today.strftime("%Y%m%d")
        paths = []
        for code in ("H00300", "000300"):
            if mode == "full":
                p = os.path.join(ARCHIVE_DIR, f"{code}-week-{day}.json")
                rows = tr_rows if code == "H00300" else px_rows
            else:
                p = os.path.join(ARCHIVE_DIR, f"{code}-incr-{day}.json")
                rows = incr_rows[code]
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"date": day, "indexCode": code, "rows": rows}, f, ensure_ascii=False)
            paths.append(p)
        # 滚动清理（git 树体积可控）
        for code in ("H00300", "000300"):
            for pat, keep in ((f"{code}-incr-*.json", KEEP_INCR), (f"{code}-week-*.json", KEEP_WEEK)):
                for p in sorted(glob.glob(os.path.join(ARCHIVE_DIR, pat)))[:-keep]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return df, tr_rows, px_rows, paths
    return df, tr_rows, px_rows, None


def bj_now():
    """北京时间（UTC+8）时间戳，用于页面展示。dry-run 固定值保证两次生成逐位一致（幂等断言）。"""
    if _DRY:
        return "dry-run"
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


def render(hs_payload, prev_html=None, tpl=None):
    """生成自包含 index.html：注入 hs300 数据段，并把现有 index.html 里的
    snapshot（红利低波）与 sector（行业轮动）数据段原样携带回来（互不覆盖）。"""
    tpl = tpl or open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    payload = {"hs300": hs_payload}
    if prev_html:
        prev = payload_util.extract(prev_html)
        if prev:
            for k in ("snapshot", "backtest", "sector"):
                if prev.get(k) is not None:
                    payload[k] = prev[k]
    return payload_util.inject(tpl, payload)


def main():
    print(f"[1/4] 抓取行情（参数：X_UP={P.X_UP} / Y_DOWN={P.Y_DOWN} / HOLD_DAYS={P.HOLD_DAYS}）...")
    df_all, tr_rows, px_rows, raw_paths = load_prices(archive=True)
    print(f"      共 {len(df_all)} 条，最新 {df_all['date'].iloc[-1].date()}（原始响应已存档 data/）")
    warnings = validate_data(df_all)
    for w in warnings:
        print("  [警告]", w)
    if raw_paths:
        day = datetime.date.today().strftime("%Y%m%d")
        snap_path = os.path.join(ARCHIVE_DIR, f"hs300-snapshot-{day}.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump({"date": day, "tr_n": len(tr_rows), "px_n": len(px_rows),
                       "rows": len(df_all), "last": df_all['date'].iloc[-1].strftime("%Y-%m-%d")}, f, ensure_ascii=False)
        print(f"      输入快照存档：{snap_path}")
    print("[2/4] 统一引擎：信号(px, 含warm-up) + T+1 撮合 + 净值核算...")
    df_all = E.build_signals(df_all, p=P)
    if P.VAL_GATE:
        sp = df_all["spread_pct"].dropna()
        if len(sp) < len(df_all) * 0.5:
            raise RuntimeError(f"估值剪刀差分位数缺失 {int(len(df_all)-len(sp))}/{len(df_all)} 行（cn10y 未刷新？），拒绝发布")
    trades, closed, positions, state, pos, legs, t0 = E.replay(df_all, t1=True, start=START, p=P)
    df = df_all[df_all["date"] >= pd.Timestamp(START)].reset_index(drop=True)
    ec = E.equity_curve(df, trades, positions, p=P)
    m = E.metrics(ec, trades)
    os_stat = E.oversold_stats(closed)
    print(f"      当前状态 {state}，仓位 {int(pos*100)}% | 回测 {m['total']*100:+.1f}% / 夏普 {m['sharpe']:.2f} / 回撤 {m['mdd']*100:.1f}% / {m['n_trades']}笔")
    print("[3/4] 生成 hs300 数据段（snapshot + backtest）...")
    bt = build_backtest_payload(df, trades, ec, m, os_stat, E.overview_stats(trades, closed, df))
    snap = build_snapshot(df_all, state, pos, legs, t0, trades, os_stat, warnings, cal=trade_day_info(), p=P)
    hs_payload = {"snapshot": snap, "backtest": bt, "params": {"x_up": P.X_UP, "y_down": P.Y_DOWN,
                                                               "hold_days": P.HOLD_DAYS}}
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump(hs_payload, f, ensure_ascii=False)
    print(f"      {DATA_OUT} ({len(bt['dates'])} 采样点 / {len(trades)} 笔 / {bt['end']})")
    print("[4/4] 合并注入 index.html（携带红利低波/行业轮动段）...")
    prev_html = None
    if os.path.exists(OUT) and not _DRY:
        with open(OUT, encoding="utf-8") as f:
            prev_html = f.read()
    html = render(hs_payload, prev_html=prev_html)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")


if __name__ == "__main__":
    main()
