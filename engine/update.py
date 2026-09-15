#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""红利低波 v7.7 策略 · 每日 8 点自动更新
抓取 H20269(全收益)/H30269(价格) 行情 -> 统一回测引擎(backtest/engine.py) 计算信号与净值
-> 每日重生成日频 backtest_data.json -> 生成自包含 index.html

口径（v7.7，与回测引擎完全一致）：
  信号用价格指数 H30269 计算；收益用全收益 H20269；T+1 收盘成交 + 单边滑点 5bp；
  杠杆部分(125/150%)按年化 7% 按交易日计息；费用单边 max(万1, 5元)。

用法: python3 update.py [--out index.html] [--data-out backtest_data.json]
"""
import json, os, sys, time, datetime, glob
import urllib.request
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import payload_util
# 交易日历（2013-2026，akshare sina；2027 年起需在 2026 年底重新拉取刷新）
_CAL = os.path.join(BASE, "trade_calendar.csv")
TRADE_DAYS = set(pd.read_csv(_CAL, parse_dates=["trade_date"])["trade_date"].dt.date.tolist()) if os.path.exists(_CAL) else None
# 统一回测引擎：优先仓库内 backtest/（同级目录），无则回退上级 backtest/
_ENGINE_DIR = os.path.join(BASE, "backtest")
if not os.path.isdir(_ENGINE_DIR):
    _ENGINE_DIR = os.path.join(os.path.dirname(BASE), "backtest")
sys.path.insert(0, _ENGINE_DIR)
import engine as E
# 【v7.12】稳健 argv 解析（原位置敏感解析会静默吞掉 --data-out 等错序参数）
_ARGS = sys.argv[1:]
_OUT = "index.html"; _DATA_OUT = "backtest_data.json"; _DRY = False
while _ARGS:
    a = _ARGS.pop(0)
    if a == "--out" and _ARGS:
        _OUT = _ARGS.pop(0)
    elif a == "--data-out" and _ARGS:
        _DATA_OUT = _ARGS.pop(0)
    elif a == "--dry-run":
        _DRY = True   # 显式 dry-run：配合 --out/--data-out 指向临时路径使用，不覆盖仓库产物、不携带线上段
OUT = os.path.join(BASE, _OUT)
DATA_OUT = os.path.join(BASE, _DATA_OUT)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
START = E.START
LOOKBACK_DAYS = E.LOOKBACK_DAYS
# 【v7.12】固定左边界锚定：不再 today-LOOKBACK（左边界每天前移导致窗口不可复核），
# 锚定中证接口 H20269 最早可回溯日 20130719，让每天的数据严格是前一天的超集。
FETCH_START = "20130719"
ARCHIVE_DIR = os.path.join(BASE, "data")


def fetch_index(code, start, end, retries=6):
    if start > end:
        return []   # 增量区间为空（基线已是最新）：合法无数据，不请求
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            rows = j.get("data") or []
            if rows or start != FETCH_START:
                return rows   # 增量区间为空（节假日/未刷新）也合法返回
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.5 * k + 0.5 * ((k * 7919) % 10))   # 指数退避 + 抖动（CI 境外 IP 偶发连接重置）
    raise RuntimeError(f"fetch CSI {code} failed: {last}")


# ---- 增量更新（v7.15）：基线=最近全量周归档 + 其后每日增量，每次只抓增量区间 ----
FULL_EVERY_DAYS = 7     # 基线陈旧超该天数 → 全量重抓
KEEP_INCR = 30          # 增量归档保留个数
KEEP_WEEK = 2           # 全量周归档保留个数


def _hl_archive_date(name, code):
    # 兼容 H20269-week-YYYYMMDD.json / H20269-incr-YYYYMMDD.json / 旧 H20269-YYYYMMDD.json
    return datetime.datetime.strptime(
        os.path.basename(name).split(f"{code}-")[1].split(".")[0].split("-")[-1], "%Y%m%d").date()


def rebuild_hl():
    """重建最近完整基线（H20269/H30269）：周归档全量 + 其后每日增量合并（确定性）。
    兼容旧格式 H20269-YYYYMMDD.json（一次性迁移基线）。
    返回 ({code: rows}, base_day)；无任何归档返回 (None, None)。"""
    out = {}
    base_day = None
    for code in ("H20269", "H30269"):
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
            # 旧全量格式 H20269-YYYYMMDD.json（迁移基线）
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
    """在线抓取 H20269(全收益) 与 H30269(价格)：增量模式（基线+增量区间合并，指数无前复权问题，
    拼接安全）；无基线或基线陈旧 → 全量。原始响应按类型归档 data/（周全量 / 日增量，供复现核对）。
    返回 (df, raw, paths)。"""
    import glob
    today = datetime.date.today()
    end = today.strftime("%Y%m%d")
    base, base_day = rebuild_hl()
    mode = "full"
    incr_rows = None
    if base is None:
        print("[增量] 无基线归档，全量抓取（首次）")
        tr_rows = fetch_index("H20269", FETCH_START, end)
        px_rows = fetch_index("H30269", FETCH_START, end)
    elif (today - base_day).days > FULL_EVERY_DAYS:
        print(f"[增量] 基线 {base_day} 陈旧 >{FULL_EVERY_DAYS} 天，全量重抓建立新周基线")
        tr_rows = fetch_index("H20269", FETCH_START, end)
        px_rows = fetch_index("H30269", FETCH_START, end)
    else:
        mode = "incr"
        start = (base_day + datetime.timedelta(days=1)).strftime("%Y%m%d")
        if start > end:
            print(f"[增量] 基线 {base_day} 已是最新，无增量区间，沿用基线")
            tr_rows, px_rows = base["H20269"], base["H30269"]
            incr_rows = {"H20269": [], "H30269": []}
        else:
            print(f"[增量] 基线 {base_day}，只抓 {start} 至今的增量区间")
            tr_new = fetch_index("H20269", start, end)
            px_new = fetch_index("H30269", start, end)
            tr = {r["tradeDate"]: r for r in base["H20269"]}
            for r in tr_new:
                tr[r["tradeDate"]] = r
            px = {r["tradeDate"]: r for r in base["H30269"]}
            for r in px_new:
                px[r["tradeDate"]] = r
            tr_rows = [tr[k] for k in sorted(tr)]
            px_rows = [px[k] for k in sorted(px)]
            incr_rows = {"H20269": tr_new, "H30269": px_new}
    tr = {r["tradeDate"]: r["close"] for r in tr_rows}
    px = {r["tradeDate"]: r["close"] for r in px_rows}
    dates = sorted(set(tr) & set(px))
    df = pd.DataFrame({"date": pd.to_datetime(dates), "close": [tr[d] for d in dates], "px": [px[d] for d in dates]})
    df = df.sort_values("date").reset_index(drop=True)
    # 注意：不截断到 START——保留 START 前的指标 warm-up（与 v7.6 一致，见 engine.get_prices 说明）
    if archive:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        day = today.strftime("%Y%m%d")
        paths = []
        for code in ("H20269", "H30269"):
            if mode == "full":
                p = os.path.join(ARCHIVE_DIR, f"{code}-week-{day}.json")
                rows = tr_rows if code == "H20269" else px_rows
            else:
                p = os.path.join(ARCHIVE_DIR, f"{code}-incr-{day}.json")
                rows = incr_rows[code]
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"date": day, "indexCode": code, "rows": rows}, f, ensure_ascii=False)
            paths.append(p)
        # 滚动清理（git 树体积可控）
        for code in ("H20269", "H30269"):
            for pat, keep in ((f"{code}-incr-*.json", KEEP_INCR), (f"{code}-week-*.json", KEEP_WEEK)):
                for p in sorted(glob.glob(os.path.join(ARCHIVE_DIR, pat)))[:-keep]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return df, tr_rows, px_rows, paths
    return df, tr_rows, px_rows, None


def trade_day_info(today=None):
    """交易日历闸门：今天是否交易日、下个交易日、数据日期陈旧天数。
    返回 dict 供页面横幅与卡片措辞使用；交易日历缺失时降级（仅周末判断）。"""
    today = today or datetime.date.today()
    if TRADE_DAYS is None:
        return {"is_today_trade": today.weekday() < 5, "next_trade": None,
                "cal_missing": True}
    nxt = today
    while nxt not in TRADE_DAYS:
        nxt += datetime.timedelta(days=1)
        if nxt > today + datetime.timedelta(days=14):   # 安全阀（日历异常时不无限循环）
            break
    return {"is_today_trade": today in TRADE_DAYS, "next_trade": nxt.strftime("%Y-%m-%d"),
            "cal_missing": False}


def validate_data(df, today=None, hard=True):
    """数据完整性校验（v7.12 升级为硬闸门）：
    - 收盘价强转 float，拒 NaN/<=0
    - 最新数据日必须是交易日（交易日历），且不得晚于最近一个交易日（防盘中占位值）
    - 重复日期键、晚于今日的行直接拒绝
    - 两序列日期并集差异
    hard=True 时校验失败 raise（宁可 job 红、保留昨日页面，不用残缺数据重算发布）；
    软警告（滞后 >3 天等）仍返回列表供页面展示。"""
    warns = []
    today = today or datetime.date.today()
    # ---- 硬校验 ----
    try:
        df["close"] = df["close"].astype(float)
        df["px"] = df["px"].astype(float)
    except (ValueError, TypeError) as e:
        raise ValueError(f"收盘价无法转 float：{e}") if hard else None
    if df["close"].isna().any() or df["px"].isna().any() or (df["close"] <= 0).any() or (df["px"] <= 0).any():
        bad_n = int(df["close"].isna().sum() + df["px"].isna().sum() + (df["close"] <= 0).sum() + (df["px"] <= 0).sum())
        if hard:
            raise ValueError(f"收盘价存在 {bad_n} 个 NaN/非正值，拒绝发布")
        warns.append(f"收盘价存在 {bad_n} 个 NaN/非正值")
    if df["date"].duplicated().any():
        if hard:
            raise ValueError(f"存在 {int(df['date'].duplicated().sum())} 个重复日期键，拒绝发布")
        warns.append("存在重复日期键")
    if TRADE_DAYS is not None:
        last = df["date"].iloc[-1].date()
        if last > today:
            if hard:
                raise ValueError(f"最新数据日 {last} 晚于今天 {today}（疑似盘中占位），拒绝发布")
        if last not in TRADE_DAYS:
            if hard:
                raise ValueError(f"最新数据日 {last} 不是交易日（交易日历），拒绝发布")
        # 数据日不得早于最近交易日超 10 个自然日
        recent = today
        while recent not in TRADE_DAYS:
            recent -= datetime.timedelta(days=1)
        stale_hard = (recent - last).days
        if stale_hard > 10:
            if hard:
                raise ValueError(f"最新数据日 {last} 距最近交易日 {recent} 达 {stale_hard} 天，拒绝发布")
    # ---- 软警告 ----
    last = df["date"].iloc[-1].date()
    stale = (today - last).days
    if stale > 10:
        warns.append(f"数据陈旧：最新数据 {last}，距今天 {stale} 天（>10 天，疑似接口缺最新交易日）")
    elif stale > 3:
        warns.append(f"数据滞后：最新数据 {last}，距今天 {stale} 天（非交易日属正常，若为工作日请留意）")
    # 两序列缺失差异：交易日只应相差节假（春节等），差异>5 天提示
    if "tr_date" in df.columns or "px_date" in df.columns:
        pass
    # 异常值：收盘价须为正
    bad = df[(df["close"] <= 0) | (df["px"] <= 0)]
    if len(bad):
        warns.append(f"检测到 {len(bad)} 个非正收盘价（{bad['date'].iloc[0].date()} 起），数据异常")
    if df["close"].isna().any() or df["px"].isna().any():
        warns.append("收盘价存在空值")
    # 单日异常跳变（>25%，除权除息或接口错误）
    chg = (df["close"].pct_change().abs())
    sp = df.loc[chg > 0.25, "date"]
    if len(sp):
        warns.append(f"全收益指数单日变动>25% 的日期 {len(sp)} 个（{sp.iloc[0].date()}…，疑似数据错误）")
    return warns


def bj_now():
    """北京时间（UTC+8）时间戳，用于页面展示。dry-run 固定值保证两次生成逐位一致（幂等断言）。"""
    if _DRY:
        return "dry-run"
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M")


def build_snapshot(df, state, pos, legs, t0, trades, os_stat, warnings=None, cal=None, p=None):
    P = p or E   # p=None 用默认参数（红利低波）；变体（沪深300）传 make_params 结果
    """今日快照：信号指标(px)、当前状态、触发缺口、临时仓倒计时。"""
    r = df.iloc[-1]
    close = float(r["close"])          # 全收益收盘（收益口径）
    px = float(r["px"])                # 价格收盘（信号口径）
    wj = float(r["wj"]); wrsi = float(r["wrsi"]); rsi = float(r["rsi"])
    upper = float(r["upper"]); lower = float(r["lower"])
    up63 = float(r["up63"]); dn63 = float(r["dn63"])
    last_date = r["date"]
    # 超卖缺口（2-of-4，px）——逐条件列出，不合成单数：
    # 2-of-4 只需再中 (2-已命中) 个；hi63 用"明日窗口"推演（明天的 rolling(63).max().shift(1)
    # 会把今天纳入、挤掉最老一天 → 阈值 = max(hi63, px)）
    need_band_low = max(0.0, (px / lower - 1) * 100)
    hi63_next = max(float(r["hi63"]), px)
    need_dn = max(0.0, (1 - hi63_next * (1 - P.Y_DOWN / 100) / px) * 100)
    hits_os = int((wj < P.J_LOW) + (px <= lower) + (float(r["dn63"]) <= -P.Y_DOWN) + (wrsi < P.RSI_OS))
    os_gap = {
        "hits": hits_os, "need": max(0, 2 - hits_os),
        "band": {"gap": round(need_band_low, 2), "hit": bool(px <= lower)},
        "dn63": {"gap": round(need_dn, 2), "hit": bool(float(r["dn63"]) <= -P.Y_DOWN)},
        "wj": {"val": round(wj, 2), "hit": bool(wj < P.J_LOW)},
        "wrsi": {"val": round(wrsi, 2), "hit": bool(wrsi < P.RSI_OS)},
        "hi63_next": round(hi63_next, 2),
    }
    # A态超买缺口（三维极值，px）
    need_band_up = max(0.0, (upper / px - 1) * 100)
    need_up = max(0.0, (float(r["lo63"]) * (1 + P.X_UP / 100) / px - 1) * 100)
    ob_gap = max(need_band_up, need_up)
    # 动能消失缺口（临时仓卖点，px）
    need_j_drop = max(0.0, wj - P.J_CROSS_TO)
    need_rsi_drop = max(0.0, wrsi - P.RSI_CROSS_TO)
    legs_info = []
    for li, ld, lf in legs:
        due_date = ld + datetime.timedelta(days=P.HOLD_DAYS)
        remaining = max(0, (due_date - last_date).days)
        legs_info.append({"buy_date": ld.strftime("%Y-%m-%d"),
                          "due_date": due_date.strftime("%Y-%m-%d"),
                          "remaining_days": remaining, "pct": 25})
    exit_info = None
    if state == "B" and t0 is not None:
        rebuy = t0 + datetime.timedelta(days=P.REBUY_DAYS)
        exit_info = {"exit_date": t0.strftime("%Y-%m-%d"),
                     "rebuy_due": rebuy.strftime("%Y-%m-%d"),
                     "remaining_days": max(0, (rebuy - last_date).days)}
    snap = {
        "generated_at": bj_now(),
        "data_date": last_date.strftime("%Y-%m-%d"),
        "cal": cal or {"is_today_trade": None, "next_trade": None, "cal_missing": True},
        "close": round(close, 2), "px": round(px, 2),
        "ma200": round(float(r["ma200"]), 2), "upper": round(upper, 2), "lower": round(lower, 2),
        "wj": round(wj, 2), "wrsi": round(wrsi, 2), "rsi": round(rsi, 2),
        "up63": round(up63, 2), "dn63": round(dn63, 2),
        "state": state, "pos": int(round(pos * 100)),
        "oversold_now": bool(r["oversold"]), "overbought_now": bool(r["overbought"]),
        "momentum_lost_now": bool(r["momentum_lost"]),
        "os_gap": os_gap,
        "ob_gap": {"need_rise_pct": round(ob_gap, 2),
                   "band_gap": round(need_band_up, 2), "up63_gap": round(need_up, 2),
                   "wj_val": round(wj, 2), "wj_ok": wj > P.J_HIGH},
        "ml_gap": {"j_need_drop": round(need_j_drop, 2), "rsi_need_drop": round(need_rsi_drop, 2),
                   "j_cross_ok": bool(r["j_cross"]), "rsi_cross_ok": bool(r["rsi_cross"]),
                   "diverg_ok": bool(r["diverg"]),
                   "hi10c": round(float(r["hi10c"]), 2), "hi10rsi": round(float(r["hi10rsi"]), 2)},
        "val": {"spread_pct": round(float(r["spread_pct"]) * 100, 1) if not pd.isna(r["spread_pct"]) else None,
                "div_proxy": round(float(r["div_proxy"]), 2) if not pd.isna(r["div_proxy"]) else None,
                "y10": round(float(r["y10"]), 3) if not pd.isna(r["y10"]) else None,
                "spread": round(float(r["spread"]), 2) if not pd.isna(r["spread"]) else None,
                "half": bool(r["os_half"]),
                "gate_ok": (not pd.isna(r["spread_pct"])) and float(r["spread_pct"]) >= 0.5,
                "ma250": round(float(r["ma250"]), 2),
                "ma250_ok": float(r["px"]) < float(r["ma250"])},
        "legs": legs_info, "exit": exit_info,
        "warnings": warnings or [],
        "recent_trades": trades[-12:],
        "oversold_stat": os_stat,
        "param": {"j_low": P.J_LOW, "j_high": P.J_HIGH, "j_cross_from": P.J_CROSS_FROM,
                  "j_cross_to": P.J_CROSS_TO, "rsi_os": P.RSI_OS,
                  "rsi_cross_from": P.RSI_CROSS_FROM, "rsi_cross_to": P.RSI_CROSS_TO,
                  "x_up": P.X_UP, "y_down": P.Y_DOWN, "hold_days": P.HOLD_DAYS, "rebuy_days": P.REBUY_DAYS,
                  "slippage_bps": P.SLIPPAGE_BPS, "fin_rate": P.FIN_RATE,
                  "val_gate": P.VAL_GATE, "ma250_gate": P.MA250_GATE, "val_win": P.VAL_WIN},
    }
    return snap


def build_backtest_payload(df, trades, ec, metrics, os_stat, overview):
    """每日重生成 backtest_data.json：日频全量净值/回撤 + 交易 + 指标 + 抄底胜率。
    指标一律用日频全量计算（抽稀仅用于页面图表展示，不用于任何指标）。"""
    n = len(ec["dates"])
    step = max(1, n // 950)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return {
        "generated_at": bj_now(),
        "start": ec["dates"][0], "end": ec["dates"][-1],
        "dates": [ec["dates"][i] for i in idx],
        "strategy_nav": [round(float(ec["strategy_nav"][i]), 6) for i in idx],
        "bh_nav": [round(float(ec["bh_nav"][i]), 6) for i in idx],
        "strategy_dd": [round(float(ec["strategy_dd"][i]), 6) for i in idx],
        "bh_dd": [round(float(ec["bh_dd"][i]), 6) for i in idx],
        "trades": trades,
        "metrics": metrics,
        "oversold_stat": os_stat,
        "overview": overview,
    }


def render(snap, bt, prev_html=None, tpl=None):
    """生成自包含 index.html：注入红利低波 payload，并把现有 index.html 里的
    sector（行业轮动）与 hs300（沪深300 择时）数据段原样携带回来
    （三个更新脚本轮流重写同一文件，互不覆盖）。"""
    tpl = tpl or open(os.path.join(BASE, "index_template.html"), encoding="utf-8").read()
    payload = {"snapshot": snap, "backtest": bt}
    if prev_html:
        prev = payload_util.extract(prev_html)
        if prev and prev.get("sector") is not None:
            payload["sector"] = prev["sector"]
        if prev and prev.get("hs300") is not None:
            payload["hs300"] = prev["hs300"]
    return payload_util.inject(tpl, payload)


def main():
    print("[1/4] 抓取行情...")
    df_all, tr_rows, px_rows, raw_paths = load_prices(archive=True)
    print(f"      共 {len(df_all)} 条，最新 {df_all['date'].iloc[-1].date()}（原始响应已存档 data/）")
    warnings = validate_data(df_all)
    for w in warnings:
        print("  [警告]", w)
    # 原始响应不可变存档：snapshot 级（当日完整输入）
    if raw_paths:
        day = datetime.date.today().strftime("%Y%m%d")
        snap_path = os.path.join(ARCHIVE_DIR, f"snapshot-{day}.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump({"date": day, "tr_n": len(tr_rows), "px_n": len(px_rows),
                       "rows": len(df_all), "last": df_all['date'].iloc[-1].strftime("%Y-%m-%d")}, f, ensure_ascii=False)
        print(f"      输入快照存档：{snap_path}")
    print("[2/4] 统一引擎：信号(px, 含warm-up) + T+1 撮合 + 净值核算...")
    df_all = E.build_signals(df_all)
    # 【v7.12】估值门数据大面积缺失时直接红掉：禁止 VAL_GATE 静默把超卖信号关成 0 后仍发布绿页面
    if E.VAL_GATE:
        sp = df_all["spread_pct"].dropna()
        if len(sp) < len(df_all) * 0.5:
            raise RuntimeError(f"估值剪刀差分位数缺失 {int(len(df_all)-len(sp))}/{len(df_all)} 行（cn10y 未刷新？），拒绝发布")
    trades, closed, positions, state, pos, legs, t0 = E.replay(df_all, t1=True, start=E.START)
    df = df_all[df_all["date"] >= pd.Timestamp(E.START)].reset_index(drop=True)
    ec = E.equity_curve(df, trades, positions)
    m = E.metrics(ec, trades)
    os_stat = E.oversold_stats(closed)
    print(f"      当前状态 {state}，仓位 {int(pos*100)}% | 回测 {m['total']*100:+.1f}% / 夏普 {m['sharpe']:.2f} / 回撤 {m['mdd']*100:.1f}% / {m['n_trades']}笔")
    print("[3/4] 重生成日频 backtest_data.json...")
    bt = build_backtest_payload(df, trades, ec, m, os_stat, E.overview_stats(trades, closed, df))
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump(bt, f, ensure_ascii=False)
    print(f"      {DATA_OUT} ({len(bt['dates'])} 采样点 / {len(trades)} 笔 / {bt['end']})")
    print("[4/4] 生成 HTML...")
    snap = build_snapshot(df_all, state, pos, legs, t0, trades, os_stat, warnings, cal=trade_day_info())
    prev_html = None
    if os.path.exists(OUT) and not _DRY:
        with open(OUT, encoding="utf-8") as f:
            prev_html = f.read()
    html = render(snap, bt, prev_html=prev_html)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      index.html 已生成 ({len(html.encode('utf-8'))//1024} KB) -> {OUT}")


if __name__ == "__main__":
    main()
