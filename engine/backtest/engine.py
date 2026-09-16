#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7.7 统一回测引擎（可复现 · 产品与回测共用单一实现）

口径修正（相对 v7.6）：
  1. 信号全部用【价格指数 H30269】计算（布林/RSI/KDJ/63日动量），收益用【全收益指数 H20269】——
     消除 TR 指数分红再投向上漂移对择时信号的污染（原 px 抓了不用）。
  2. 执行模型改为 T+1：T 日收盘确认信号，T+1 日收盘价成交。
     披露：数据源无开盘价，以 T+1 收盘近似 T+1 开盘；另加单边滑点 5bp 保守化。
  3. 融资成本：仓位 >100% 的杠杆部分（25%/50%）按年化 7% 按交易日计息（0.07/252/日）。
  4. 费用：单边 max(成交额×万1, 5 元)。
  5. 输出日频全量净值/回撤（2427 个交易日不抽稀），指标在引擎内计算，杜绝静态产物误导。

用法:
  python3 engine.py [--out-dir .] [--start 2016-09-08]   # 独立回测（读 ../backtest 本地 CSV）
  或作为模块被 update.py import（get_prices -> build_signals -> replay -> metrics）
"""
import json, os, sys, math, datetime, types
import pandas as pd
import numpy as np

# ============ 参数（v7.7 与 v7.6 一致的信号参数，执行口径修正） ============
HOLD_DAYS = 60            # 临时仓到期卖出：60 个自然日
REBUY_DAYS = 90           # A态清仓后强制回补上限：90 个自然日（v7.9: 60→90，减少L型磨底接飞刀）
J_LOW, J_HIGH = 1.0, 95.0
J_CROSS_FROM, J_CROSS_TO = 90.0, 80.0
RSI_OS = 35.0
RSI_CROSS_FROM, RSI_CROSS_TO = 70.0, 65.0
X_UP, Y_DOWN = 20.0, 14.0   # 超买63日涨幅阈值20% / 超卖63日跌幅阈值14%（v7.10: 15→14，walk-forward三段一致增强，夏普0.64/回撤-30.4%）
Y_ACC = False               # 实验否决：超卖跌幅加"跌幅加速"确认（近10日跌幅 ≥ 近63日跌幅×0.6，全期/WF均变差，保留代码可复现）
DIV_2OF3 = False            # 实验否决：顶背离扩展 RSI+MACD+量价 三选二（回撤恶化至-34.2%，保留代码可复现）
DELAY_SELL = 1              # 实验否决：动能消失延迟成交（T+3 回撤-32.2%无增益，默认 T+1）
# ---- v7.11 估值/年线过滤（定稿） ----
VAL_GATE = True             # 估值剪刀差(股息率代理-10Y国债)分位门: ≥80%全力 / 50-80%半力 / <50%超卖信号失效
VAL_HALF_CAP = 1.25         # 半力仓位上限；红利低波=1.25（仍可加一档杠杆），沪深300 v9.2=1.0（只回补不借钱）
MA250_GATE = True           # 年线门: 仅 价格<250日均线 时允许超卖信号（年线上方超卖=高位回调不执行）
WEEK_J0 = False             # 实验否决：周线共振门（wj<0 才允许超卖；"不动"/"半力"两档均降收益，保留代码可复现）
VAL_WIN = 3                 # 剪刀差滚动分位窗口（年；2y/3y/5y/expanding 实测：3y 收益-回撤平衡且无冷启动问题；5y 冷启动致2016-19段差）
CN10Y_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cn10y_daily.csv")
MAX_POS = 1.50
# ---- v9.0 年线底仓缩放（默认关闭=红利低波零回归；沪深300 变体 BEAR_CORE=0.5）----
# 审计结论：v8.1「年线下方仍 100% 底仓」在 2021-2023 熊市拖累夏普；
# 超卖门已要求 px<MA250，故熊市常态满仓与「适度抄底」矛盾——应先降底仓再等冰点加仓。
BEAR_CORE = 1.0           # 价格在年线下方时的底仓；1.0=不缩放
BULL_CORE = 1.0           # 价格在年线上方时的底仓
CORE_CONFIRM = 0          # 年线切换确认交易日数（0=当日切换；5=减少毛刺）
CORE_STEP = 0.25          # 超卖加仓 / 到期减仓步长
OB_FROM_CD = False        # C/D 态是否响应超买清仓；沪深300=True（加仓遇极值一并离场）
# ---- v9.1 强制回补 / 熊市超卖门槛（默认关闭=红利低波零回归；沪深300 变体开启）----
# 审计：贪婪清仓后「满 REBUY_DAYS 无条件回补」在假突破年线时接飞刀（2023-04）；
# 熊市 2-of-4 过松，浅跌加仓拖累 2021-2023。沪深300：FORCE_MIN_ABOVE_MA=20 / OS_MIN_COUNT_BEAR=3。
FORCE_MIN_ABOVE_MA = 0    # 强制回补前需连续站上 MA250 的交易日数；0=不额外要求
OS_MIN_COUNT_BEAR = 2     # 熊市（年线下方 regime）超卖最少命中数；牛市仍用 2-of-4
# H-1 修复（策略层审计）：交易日历（engine/trade_calendar.csv，列 trade_date）用于判定
# "未完成 ISO 周"——df 末日之后若仍有交易日落在同一 ISO 周，则该周未完成。
_CAL_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trade_calendar.csv")
_cal_sorted = None

def _load_cal():
    """加载并缓存交易日历（升序 date 列表）；缺失/异常返回空列表。"""
    global _cal_sorted
    if _cal_sorted is None:
        try:
            if os.path.exists(_CAL_CSV):
                raw = pd.read_csv(_CAL_CSV)
                col = "trade_date" if "trade_date" in raw.columns else "date"
                _cal_sorted = sorted(pd.to_datetime(raw[col]).dt.date.tolist())
            else:
                _cal_sorted = []
        except Exception:
            _cal_sorted = []
    return _cal_sorted

def _week_completed(last_day):
    """last_day 所在 ISO 周是否已完整（该周无未到的交易日）。
    有日历时：last_day 之后最近交易日若仍属同一 ISO 周 → 未完成；
    无日历降级：周五（weekday=4）视为完成（该周无未来交易日）。"""
    cal = _load_cal()
    if not cal:
        return last_day.weekday() >= 4
    lo, hi = 0, len(cal)
    while lo < hi:                 # bisect 右边界：第一个 > last_day 的交易日
        mid = (lo + hi) // 2
        if cal[mid] <= last_day:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(cal):
        return True                # last_day 之后无交易日 → 本周已完成
    nxt = cal[lo]
    return nxt.isocalendar()[:2] != last_day.isocalendar()[:2]
START = "2016-09-08"
LOOKBACK_DAYS = 4800      # 抓取回看（update.py 用；需覆盖 2014 起指标 warm-up）
SLIPPAGE_BPS = 5          # 单边滑点 5bp = 0.05%
FEE_RATE = 0.0001         # 万1
FEE_MIN = 5.0             # 最低 5 元
FIN_RATE = 0.07           # 融资年化 7%（>100% 杠杆部分，按交易日计息）
TRADING_DAYS = 252        # 年化基准
RISK_FREE = 0.0           # 夏普无风险利率

# ---- 参数化（v8.0/v9.x 沪深300 变体）：默认 = 上方模块常量；变体仅覆盖个别参数 ----
_PARAM_NAMES = ("HOLD_DAYS", "REBUY_DAYS", "J_LOW", "J_HIGH", "J_CROSS_FROM", "J_CROSS_TO",
                "RSI_OS", "RSI_CROSS_FROM", "RSI_CROSS_TO", "X_UP", "Y_DOWN", "Y_ACC",
                "DIV_2OF3", "DELAY_SELL", "VAL_GATE", "VAL_HALF_CAP", "MA250_GATE", "WEEK_J0", "VAL_WIN",
                "MAX_POS", "BEAR_CORE", "BULL_CORE", "CORE_CONFIRM", "CORE_STEP", "OB_FROM_CD",
                "FORCE_MIN_ABOVE_MA", "OS_MIN_COUNT_BEAR",
                "START", "SLIPPAGE_BPS", "FEE_RATE", "FEE_MIN", "FIN_RATE", "TRADING_DAYS")


def make_params(**overrides):
    """基于默认参数构造变体参数集（如沪深300 v9.2：BEAR_CORE=0.4 / VAL_HALF_CAP=1.0）。
    未覆盖项与红利低波完全一致；BEAR_CORE=1.0 时年线底仓缩放不生效。"""
    p = {k: globals()[k] for k in _PARAM_NAMES}
    p.update(overrides)
    return types.SimpleNamespace(**p)


# ============ 数据 ============
def get_prices(tr_path, px_path, start=START, end=None):
    """读本地 CSV，合并价格指数与全收益指数（两边都有的日期），日期升序。
    CSV 自 2014 年起；start 只作回测起点标记，不截断（保留 START 前的指标 warm-up，
    与 v7.6 一致——KDJ/RSI/动量/布林都需要 2014-2016 的预热数据）。
    vol（H20269 成交量）保留用于量价背离。"""
    tr = pd.read_csv(tr_path, parse_dates=["date"])
    px = pd.read_csv(px_path, parse_dates=["date"])
    # 先保留 vol（H20269 成交量），再做列切片——原实现先切片导致 "trading_vol" 恒不在列中
    if "trading_vol" in tr.columns:
        tr["vol"] = tr["trading_vol"]
    else:
        tr["vol"] = np.nan
    tr = tr[["date", "close", "vol"]].rename(columns={"close": "close"})
    px = px[["date", "close"]].rename(columns={"close": "px"})
    df = tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end)].reset_index(drop=True)
    return df


def build_signals(df, use_tr=False, p=None):
    """信号指标全部在价格指数 px 上计算；close 保留全收益用于收益核算。
    use_tr=True 时信号改用全收益序列（仅用于口径归因实验，主回测恒为 False）。
    p=None 用默认参数（红利低波口径）；p=make_params(...) 用于变体（如沪深300）。"""
    P = p or sys.modules[__name__]
    c = df["close"] if use_tr else df["px"]
    df["ma20"] = c.rolling(20).mean()
    df["std20"] = c.rolling(20).std(ddof=0)
    df["upper"] = df["ma20"] + 2 * df["std20"]
    df["lower"] = df["ma20"] - 2 * df["std20"]
    df["ma200"] = c.rolling(200).mean()
    # 周线 KDJ(9,3,3)，ISO 周（跟随信号序列 c：默认 px，use_tr=True 时为 TR）
    iso = df["date"].dt.isocalendar()
    wk_key = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    wk = df.groupby(wk_key).agg(close=(c.name, "last"), date=("date", "last")).reset_index(drop=True)
    # H-1 修复（策略层审计）：实盘/回测周线口径一致化——剔除"未完成 ISO 周"。
    # 回测中 df 末日=周五（完整周），实盘每日运行时末日=今天（未完成周），
    # 若保留最后一行，map 会命中"用不完整周算出的 J"，与回测（ffill 上一周）口径不一致。
    # 判定：df 末日之后若仍有交易日落在同一 ISO 周 → 未完成，剔除（用交易日历；无日历降级周五判断）。
    # 对回测无影响：完整历史末日=周五，不触发剔除。
    if len(wk) >= 2 and not _week_completed(df["date"].iloc[-1].date()):
        wk = wk.iloc[:-1]
    low9 = wk["close"].rolling(9).min()
    high9 = wk["close"].rolling(9).max()
    rsv = ((wk["close"] - low9) / (high9 - low9) * 100).fillna(50.0)
    k = np.empty(len(wk)); d = np.empty(len(wk)); k[0] = d[0] = 50.0
    for i in range(1, len(wk)):
        k[i] = 2 / 3 * k[i - 1] + 1 / 3 * rsv.iloc[i]
        d[i] = 2 / 3 * d[i - 1] + 1 / 3 * k[i]
    wk["J"] = 3 * k - 2 * d
    df["wj"] = df["date"].map(wk.set_index("date")["J"]).ffill()
    # 周线 RSI(14) Wilder（px）
    wk_dlt = wk["close"].diff()
    wg = wk_dlt.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wl = (-wk_dlt).clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    wk["RSI"] = (100 - 100 / (1 + wg / wl)).fillna(50.0)
    df["wrsi"] = df["date"].map(wk.set_index("date")["RSI"]).ffill()
    # 日线 RSI(14) Wilder（px，顶背离用）
    delta = c.diff()
    ru = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    rd = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + ru / rd)
    # 近63日动量（前一日窗口）
    df["lo63"] = c.rolling(63).min().shift(1)
    df["hi63"] = c.rolling(63).max().shift(1)
    df["up63"] = (c / df["lo63"] - 1) * 100
    df["dn63"] = (c / df["hi63"] - 1) * 100
    # 近10日动量（加速确认用）
    df["hi10"] = c.rolling(10).max().shift(1)
    df["dn10"] = (c / df["hi10"] - 1) * 100
    # ---- v7.11 估值剪刀差：股息率代理(TR/px 12个月滚动增长率) - 十年期国债收益率 ----
    # 代理股息率系统性偏高约0.4pp（含再投资收益），分位数形态与官方一致（2024-09 高企等），仅用于分位门
    if os.path.exists(CN10Y_CSV):
        y10 = pd.read_csv(CN10Y_CSV, parse_dates=["date"]).set_index("date")["y10"]
        df["div_proxy"] = (df["close"] / df["px"] / (df["close"].shift(252) / df["px"].shift(252)) - 1) * 100
        df["y10"] = df["date"].map(y10).ffill()
        df["spread"] = df["div_proxy"] - df["y10"]
        # v7.11 定稿：滚动分位窗口 VAL_WIN 年（2y/3y/5y/expanding 实测选 3y；5y 冷启动 min_periods>回测预热致早期信号全禁）
        win = int(P.VAL_WIN * 252)
        df["spread_pct"] = df["spread"].rolling(win, min_periods=int(win * 0.8)).rank(pct=True)
        df["os_half"] = df["spread_pct"].between(0.5, 0.8)
    else:
        # 【v7.12】cn10y 缺失时补齐全部估值字段（此前只补 spread 三件套，
        # 导致 update.py 读 div_proxy/y10 时 KeyError，job 直接红）
        df["div_proxy"] = (df["close"] / df["px"] / (df["close"].shift(252) / df["px"].shift(252)) - 1) * 100
        df["y10"] = np.nan
        df["spread"] = df["spread_pct"] = np.nan
        df["os_half"] = False
    # 超卖 2-of-4（px；v7.10 可选跌幅加速确认：dn63≤-Y_DOWN 且 近10日跌幅≥近63日跌幅×0.6）
    drop_cond = (df["dn63"] <= -P.Y_DOWN) & ((df["dn10"] <= df["dn63"] * 0.6) if P.Y_ACC else True)
    os_raw = ((df["wj"] < P.J_LOW).astype(int) + (c <= df["lower"]).astype(int)
              + drop_cond.astype(int) + (df["wrsi"] < P.RSI_OS).astype(int)) >= 2
    # v7.11 信号级过滤：估值分位<50% / 价格≥250日线 / 周线J≥0 时，超卖信号失效
    df["ma250"] = c.rolling(250).mean()
    if P.VAL_GATE:
        os_raw = os_raw & (df["spread_pct"] >= 0.5)
    if P.MA250_GATE:
        os_raw = os_raw & (c < df["ma250"])
    if P.WEEK_J0:
        os_raw = os_raw & (df["wj"] < 0)
    df["oversold"] = os_raw
    # A态超买三维极值（px）
    df["overbought"] = (df["wj"] > P.J_HIGH) & (c >= df["upper"]) & (df["up63"] >= P.X_UP)
    # 动能消失（px）
    df["hi10c"] = c.rolling(10).max().shift(1)
    df["hi10rsi"] = df["rsi"].rolling(10).max().shift(1)
    rsi_diverg = (c > df["hi10c"]) & (df["rsi"] < df["hi10rsi"])
    if P.DIV_2OF3:
        # 扩展顶背离：RSI + MACD柱 + 量价 三选二（v7.10 实验）
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        df["macd_hist"] = 2 * (dif - dea)
        df["hi10macd"] = df["macd_hist"].rolling(10).max().shift(1)
        macd_diverg = (c > df["hi10c"]) & (df["macd_hist"] < df["hi10macd"])
        vol = df["vol"].ffill()
        df["vol5"] = vol.rolling(5).mean()
        vol_diverg = (c > df["hi10c"]) & (df["vol"] < df["vol5"])
        df["diverg"] = (rsi_diverg.astype(int) + macd_diverg.astype(int) + vol_diverg.astype(int)) >= 2
    else:
        df["diverg"] = rsi_diverg
    df["j_cross"] = (df["wj"].rolling(10).max() > P.J_CROSS_FROM) & (df["wj"] <= P.J_CROSS_TO)
    df["rsi_cross"] = (df["wrsi"].rolling(10).max() > P.RSI_CROSS_FROM) & (df["wrsi"] <= P.RSI_CROSS_TO)
    df["momentum_lost"] = df["j_cross"] | df["rsi_cross"] | df["diverg"]
    return df


def replay(df, t1=True, start=START, delay_sell=DELAY_SELL, p=None):
    """T+1 撮合状态机重放（从 start 起输出；start 之前仅作信号预热）。
    信号 T 日收盘确认（用 df 上一行信号），T+1 日收盘成交（滑点计入成交价）。
    t1=False 时信号当日收盘确认、当日收盘成交（仅用于口径归因实验，主回测恒为 True）。
    delay_sell: 动能消失触发后第 N 个交易日成交（1=T+1 默认；3=延迟到第 3 交易日收盘成交，v7.10 实验）。
    p=None 用默认参数；p=make_params(...) 用于变体（如沪深300）。

    v9.0 年线底仓：BEAR_CORE<1 时，价格持续在 MA250 下方则 A 态底仓=BEAR_CORE（默认 1.0 不缩放）；
    超卖按 CORE_STEP 加仓，动能消失/到期回落到当前 regime 底仓；强制回补也回到底仓。
    v9.1：FORCE_MIN_ABOVE_MA>0 时，强制回补须连续站上 MA250 达该日数（防假突破接飞刀）；
    OS_MIN_COUNT_BEAR>2 时，熊市超卖须命中更多 2-of-4 子条件（默认 2=与牛市相同）。
    v9.2：VAL_HALF_CAP 控制半力上限——A/B/C/D 态一致生效（旧逻辑仅 C/D 限制且写死 1.25，
    导致 A 态 100%→125% 在估值仅 50-80% 时仍加杠杆；沪深300 取 1.0=只回补不借钱）。

    返回 (trades, legs_closed, positions, state, pos, legs, t0)：
      trades: 每笔 {date, action, px(信号价), fill(成交价含滑点), fee, slippage,
                    pos_before, pos_after, reason, amount}
      legs_closed: 抄底档闭环（FIFO）{buy_date, buy_fill, sell_date, sell_fill, reason, ret}
      positions: 每日目标仓位 Series（长度 = df 中 >= start 的行数）
    """
    P = p or sys.modules[__name__]
    step = float(getattr(P, "CORE_STEP", 0.25))
    bear_core = float(getattr(P, "BEAR_CORE", 1.0))
    bull_core = float(getattr(P, "BULL_CORE", 1.0))
    confirm = int(getattr(P, "CORE_CONFIRM", 0))
    ob_from_cd = bool(getattr(P, "OB_FROM_CD", False))
    force_min_above = int(getattr(P, "FORCE_MIN_ABOVE_MA", 0))
    os_min_bear = int(getattr(P, "OS_MIN_COUNT_BEAR", 2))
    half_cap = float(getattr(P, "VAL_HALF_CAP", 1.25))
    trades = []
    legs = []          # [(成交索引, 成交日期, 成交价(含滑点))]
    legs_closed = []
    start_ts = pd.Timestamp(start)
    n_out = int((df["date"] >= start_ts).sum())
    positions = np.zeros(n_out)
    k = 0
    state, pos = "A", None          # pos 首日按 regime 底仓初始化
    t0 = None          # B 态离场日
    prev = None        # T 日信号
    prev2 = None       # T-1 日信号（连续确认）
    pend = None        # 动能消失挂起: [确认行索引i, 确认日期]
    below_run = above_run = 0
    regime_bear = None

    def _want_bear(row):
        ma = row["ma250"] if "ma250" in row.index else np.nan
        if ma != ma:  # NaN
            return False
        return float(row["px"]) < float(ma)

    def _update_regime(row):
        nonlocal below_run, above_run, regime_bear
        wb = _want_bear(row)
        if wb:
            below_run += 1
            above_run = 0
        else:
            above_run += 1
            below_run = 0
        if regime_bear is None:
            regime_bear = wb
        elif confirm <= 0:
            regime_bear = wb
        else:
            if wb and below_run >= confirm:
                regime_bear = True
            if (not wb) and above_run >= confirm:
                regime_bear = False

    def _core():
        return bear_core if regime_bear else bull_core

    def _state_for(new_pos, core):
        if new_pos <= 1e-9:
            return "B"
        if new_pos <= core + 1e-9:
            return "A"
        # 杠杆上沿（≥125% 且接近 MAX）标 D，其余加仓标 C——与旧四态文案兼容
        if new_pos >= min(1.25, P.MAX_POS) - 1e-9 and new_pos >= P.MAX_POS - 1e-9:
            return "D"
        if new_pos >= min(1.50, P.MAX_POS) - 1e-9:
            return "D"
        return "C"

    def _os_count(row):
        """2-of-4 子条件命中数（与 build_signals 超卖定义一致，不含估值/年线门）。"""
        c = float(row["px"])
        return (int(float(row["wj"]) < P.J_LOW)
                + int(c <= float(row["lower"]))
                + int(float(row["dn63"]) <= -P.Y_DOWN)
                + int(float(row["wrsi"]) < P.RSI_OS))

    def _osig_ok(row):
        """超卖是否可执行：先过 build_signals 门，熊市再抬高命中数。"""
        if not bool(row["oversold"]):
            return False
        need = os_min_bear if regime_bear else 2
        return _os_count(row) >= need

    for i in range(len(df)):
        r = df.iloc[i]
        d = r["date"]
        if d < start_ts:                # START 之前仅推进 prev/regime（warm-up），不参与撮合
            _update_regime(r)
            prev2, prev = prev, r
            continue
        _update_regime(r)
        core = _core()
        if pos is None:
            pos = core
        osig = obsig = lost = False
        if t1:
            if prev is not None:                      # T 日收盘确认的信号
                osig = _osig_ok(prev); obsig = bool(prev["overbought"])
                lost = bool(prev["momentum_lost"])
        else:
            osig = _osig_ok(r); obsig = bool(r["overbought"]); lost = bool(r["momentum_lost"])
        act = None
        # H-6：仓位档位由 MAX_POS / CORE_STEP / 当前底仓派生
        if state == "A" and not legs and abs(pos - core) > 1e-6:
            # 年线切换：无临时仓时把底仓对齐到当前 regime（BEAR_CORE=1 时不触发）
            act = (("buy" if core > pos else "sell"), core, "A",
                   f"年线切换·底仓调至{int(round(core * 100))}%")
        def _half(prev_row):
            return P.VAL_GATE and prev_row is not None and bool(prev_row["os_half"])

        if act is None and state == "A":
            if osig:
                # 熊市底仓：首次超卖先回到满仓（或 +step 若已≥100%），再按步进加杠杆
                if pos < 1.0 - 1e-9:
                    new_pos = min(1.0, P.MAX_POS)
                else:
                    new_pos = min(pos + step, P.MAX_POS)
                # v9.2：半力在 A 态也生效——估值未到全力分位时不得超过 VAL_HALF_CAP
                if _half(prev) and new_pos > half_cap + 1e-9:
                    if pos < min(1.0, half_cap) - 1e-9:
                        new_pos = min(1.0, half_cap, P.MAX_POS)
                    else:
                        new_pos = pos
                if new_pos > pos + 1e-9:
                    act = ("buy", new_pos, _state_for(new_pos, core),
                           f"情绪极值超卖共振·加仓至{int(round(new_pos * 100))}%")
            elif obsig:
                act = ("sell", 0.0, "B", "超买极值共振·清仓离场")
        elif state == "B":
            if osig:
                # 空仓遇超卖：回补到满仓（熊市也先回到 100% 再谈加杠杆）
                new_pos = min(max(1.0, core + step), P.MAX_POS)
                if _half(prev):
                    new_pos = min(new_pos, half_cap)
                act = ("buy", new_pos, _state_for(new_pos, core),
                       f"离场中现超卖共振·回补并加仓至{int(round(new_pos * 100))}%")
            elif t0 is not None and d >= t0 + datetime.timedelta(days=P.REBUY_DAYS):
                # v9.1：强制回补须年线上方站稳 FORCE_MIN_ABOVE_MA 日（0=不额外要求）
                if force_min_above > 0 and above_run < force_min_above:
                    pass
                else:
                    act = ("buy", core, "A",
                           f"离场满{int(P.REBUY_DAYS)}自然日·强制回补至{int(round(core * 100))}%")
        elif state in ("C", "D"):
            # v7.11/v9.2 估值"半力"：分位 50-80% 时禁止加仓越过 VAL_HALF_CAP
            if osig and pos < P.MAX_POS - 1e-9:
                new_pos = min(pos + step, P.MAX_POS)
                if not (_half(prev) and new_pos > half_cap + 1e-9):
                    act = ("buy", new_pos, _state_for(new_pos, core),
                           f"再次超卖共振·加仓至{int(round(new_pos * 100))}%")
            lost_now = prev is not None and bool(prev["momentum_lost"])
            if lost_now and pend is None:
                pend = [i - 1, d]          # 记录确认日 T（prev 行索引 i-1）
            if pend is not None and i - pend[0] >= delay_sell:
                act = ("sell", core, "A",
                       f"动能消失·了结临时仓回{int(round(core * 100))}%")
                pend = None
            if act is None and legs and d >= legs[0][1] + datetime.timedelta(days=P.HOLD_DAYS):
                new_pos = max(pos - step, core)
                act = ("sell", new_pos, _state_for(new_pos, core),
                       f"加仓满{int(P.HOLD_DAYS)}自然日·卖出一档临时仓")
            # v9：加仓态也可超买清仓（沪深300）；红利低波默认关闭以保持零回归
            if act is None and obsig and ob_from_cd:
                act = ("sell", 0.0, "B", "超买极值共振·清仓离场")
        # 期初建仓：回测起点首日按 regime 底仓建仓
        if k == 0 and len(trades) == 0:
            act = ("buy", core, "A",
                   f"期初建底仓·{int(round(core * 100))}%")
        if act:
            new_pos, new_state = float(act[1]), act[2]
            px_ = float(r["px"])
            fill = px_ * (1 + P.SLIPPAGE_BPS / 1e4) if act[0] == "buy" else px_ * (1 - P.SLIPPAGE_BPS / 1e4)
            trades.append({"date": d.strftime("%Y-%m-%d"), "action": "买入" if act[0] == "buy" else "卖出",
                           "px": round(px_, 2), "fill": round(fill, 2), "reason": act[3],
                           "pos_before": int(round(pos * 100)), "pos_after": int(round(new_pos * 100))})
            # 临时仓：仅 >100% 的杠杆加仓记账（与 v8 一致；50%→100% 由动能消失/年线切换回落）
            if act[0] == "buy" and new_pos > 1.0 + 1e-9 and new_pos > pos + 1e-9:
                legs.append((i, d, fill))
            if act[0] == "sell":
                if new_pos <= 1e-9:
                    n_legs = len(legs)
                else:
                    # 回到 ≤100% 时清空全部杠杆腿；否则按步长剥一档
                    if new_pos <= 1.0 + 1e-9:
                        n_legs = len(legs)
                    else:
                        n_legs = int(round((pos - new_pos) / step))
                        n_legs = max(0, min(n_legs, len(legs)))
                for _ in range(n_legs):
                    if legs:
                        b_i, b_d, b_f = legs.pop(0)
                        legs_closed.append({"buy_date": b_d.strftime("%Y-%m-%d"), "buy_fill": round(b_f, 2),
                                            "sell_date": d.strftime("%Y-%m-%d"), "sell_fill": round(fill, 2),
                                            "reason": act[3],
                                            "ret": round((fill - b_f) / b_f * 100, 2)})
                if new_pos <= 1e-9:
                    while legs:
                        b_i, b_d, b_f = legs.pop(0)
                        legs_closed.append({"buy_date": b_d.strftime("%Y-%m-%d"), "buy_fill": round(b_f, 2),
                                            "sell_date": d.strftime("%Y-%m-%d"), "sell_fill": round(fill, 2),
                                            "reason": act[3],
                                            "ret": round((fill - b_f) / b_f * 100, 2)})
            pos, state = new_pos, new_state
            if state == "A":
                t0 = None
            if state == "B":
                t0 = d
        positions[k] = pos
        k += 1
        prev2, prev = prev, r
    return (trades, legs_closed, positions, state, pos, legs, t0)


def equity_curve(df, trades, positions, initial=100000.0, start=START, p=None):
    """日频净值核算（全收益 close 计收益，T+1 撮合）。
    【v7.12 地基修正】持仓市值按全收益指数再投计价：q 为 TR 归一份额
    （买入 q += buy_amt/(px*tr)，每日市值 = q * px * tr），使策略端吃到分红再投，
    与买入持有（bh_nav 用 TR）口径一致。此前用 px 计价漏掉全部分红，策略收益系统性低估。
    成交价仍用 px（可交易价格）；滑点+费用+融资成本作为显式成本从净值扣除。
    df 须为 >= start 的回测段（positions 与之对齐）。
    p=None 用默认参数；p=make_params(...) 用于变体（如沪深300）。
    返回 dict: dates/strategy_nav/bh_nav/strategy_dd/bh_dd/pos_pct/costs。"""
    P = p or sys.modules[__name__]
    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    tr = df["close"].values          # 全收益指数（收益口径）
    n = len(df)
    cash = initial
    q = 0.0            # TR 归一份额：市值 = q * tr（分红再投计入，见下）
    nav_ts = np.zeros(n)
    costs_ts = np.zeros(n)
    pos_pct = np.zeros(n)
    t_by_date = {t["date"]: t for t in trades}
    s = P.SLIPPAGE_BPS / 1e4
    for i in range(n):
        dstr = dates[i]
        tr_i = tr[i]
        hold_val = q * tr_i           # 持仓市值：买入额按全收益指数增长率增值（含分红再投）
        t = t_by_date.get(dstr)
        if t is not None:
            if t["action"] == "买入":
                cur_val = cash + hold_val
                target_val = cur_val * (t["pos_after"] / 100.0)
                buy_amt = max(0.0, target_val - hold_val)
                if buy_amt > 0:
                    fee = max(buy_amt * P.FEE_RATE, P.FEE_MIN)
                    slip = buy_amt * s
                    q += buy_amt / tr_i
                    cash -= buy_amt
                    costs_ts[i] += fee + slip
            else:
                cur_val = cash + hold_val
                target_val = cur_val * (t["pos_after"] / 100.0)
                sell_amt = max(0.0, hold_val - target_val)
                if sell_amt > 0:
                    fee = max(sell_amt * P.FEE_RATE, P.FEE_MIN)
                    slip = sell_amt * s
                    q -= sell_amt / tr_i
                    cash += sell_amt
                    costs_ts[i] += fee + slip
        # 融资成本（持仓日计提）：杠杆部分按日计息
        pct = positions[i]
        if pct > 1.0 + 1e-9:
            val = cash + q * tr_i
            costs_ts[i] += val * (pct - 1.0) * P.FIN_RATE / P.TRADING_DAYS
        pos_pct[i] = pct * 100
        nav_ts[i] = cash + q * tr_i
    # 显式成本在净值中扣除（等价于每日从收益扣减）
    cum_cost = np.cumsum(costs_ts)
    strategy_nav = (nav_ts - cum_cost) / initial
    bh_nav = df["close"].values / df["close"].iloc[0]
    dd_s = strategy_nav / np.maximum.accumulate(strategy_nav) - 1
    dd_b = bh_nav / np.maximum.accumulate(bh_nav) - 1
    return {"dates": dates, "strategy_nav": strategy_nav, "bh_nav": bh_nav,
            "strategy_dd": dd_s, "bh_dd": dd_b, "pos_pct": pos_pct, "costs": costs_ts}


def metrics(ec, trades, initial=100000.0):
    """年化/夏普/CAGR/最大回撤/费用/滑点/利息，全部在日频序列上计算。"""
    nav = ec["strategy_nav"]
    bh = ec["bh_nav"]
    n = len(nav)
    years = n / TRADING_DAYS
    total = nav[-1] - 1
    cagr = nav[-1] ** (1 / years) - 1
    rets = np.diff(nav) / nav[:-1]
    sharpe = (rets.mean() / rets.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    total_bh = bh[-1] - 1
    cagr_bh = bh[-1] ** (1 / years) - 1
    rets_bh = np.diff(bh) / bh[:-1]
    sharpe_bh = (rets_bh.mean() / rets_bh.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets_bh) > 1 and rets_bh.std(ddof=1) > 0 else 0.0
    fee = sum(t.get("fee", 0) for t in trades)
    return {"total": float(total), "ann": float(cagr), "sharpe": float(sharpe),
            "mdd": float(dd_min(ec["strategy_dd"])), "n_trades": len(trades),
            "total_fee": round(float(ec["costs"].sum()), 2),
            "total_bh": float(total_bh), "ann_bh": float(cagr_bh), "sharpe_bh": float(sharpe_bh),
            "mdd_bh": float(dd_min(ec["bh_dd"])),
            "final_value": float(nav[-1] * initial), "final_bh": float(bh[-1] * initial),
            "fee_total": round(float(ec["costs"].sum()), 2)}


def dd_min(dd_series):
    return float(np.min(dd_series)) if len(dd_series) else 0.0


def oversold_stats(closed):
    """抄底胜率（FIFO 闭环，按买入年份/卖出原因分组）。"""
    st = {"total": len(closed), "wins": sum(1 for c in closed if c["ret"] > 0),
          "losses": sum(1 for c in closed if c["ret"] <= 0), "closed": closed}
    if not closed:
        st["winrate"], st["avg_ret"] = 0.0, 0.0
        st["yearly"], st["reason"] = [], []
        return st
    st["winrate"] = round(st["wins"] / len(closed) * 100, 1)
    st["avg_ret"] = round(sum(c["ret"] for c in closed) / len(closed), 2)
    by_year, by_reason = {}, {}
    for c in closed:
        by_year.setdefault(c["buy_date"][:4], []).append(c)
        key = "到期强制卖出" if "到期" in c["reason"] else ("动能消失·顶背离" if "顶背离" in c["reason"]
                              else ("动能消失·J跌破80" if "J跌破80" in c["reason"]
                                    else ("动能消失·RSI跌破65" if "RSI跌破65" in c["reason"] else c["reason"])))
        by_reason.setdefault(key, []).append(c)
    st["yearly"] = []
    for y in sorted(by_year):
        g = by_year[y]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
        st["yearly"].append({"year": y, "n": n, "wins": w, "losses": n - w,
                             "winrate": round(w / n * 100, 1),
                             "avg_ret": round(sum(c["ret"] for c in g) / n, 2), "closed": g})
    st["reason"] = []
    for k in by_reason:
        g = by_reason[k]; n = len(g); w = sum(1 for c in g if c["ret"] > 0)
        st["reason"].append({"reason": k, "n": n, "wins": w, "losses": n - w,
                             "winrate": round(w / n * 100, 1),
                             "avg_ret": round(sum(c["ret"] for c in g) / n, 2)})
    st["reason"].sort(key=lambda x: -x["n"])
    return st


def overview_stats(trades, closed, df):
    """策略概览（给用户全貌与预期）：满仓+超跌抄底的操作频率统计。
    返回 dict: years / add_total / add_per_year / clear_total / clear_per_year /
               temp_sell_per_year / avg_hold_days / by_year / pct125 / pct150 /
               os_total / os_winrate / os_avg_ret / first / last"""
    n_days = (df["date"].iloc[-1] - df["date"].iloc[0]).days
    years = max(n_days / 365.25, 1e-9)
    add = [t for t in trades if t["action"] == "买入" and t["pos_after"] > 100 and "期初" not in t["reason"]]
    clear = [t for t in trades if t["action"] == "卖出" and t["pos_after"] == 0]
    temp_sell = [t for t in trades if t["action"] == "卖出" and t["pos_after"] == 100 and t["pos_before"] > 100]
    by_year = {}
    for t in add:
        by_year[t["date"][:4]] = by_year.get(t["date"][:4], 0) + 1
    hold_days = []
    for c in closed:
        b = pd.Timestamp(c["buy_date"]); s = pd.Timestamp(c["sell_date"])
        hold_days.append((s - b).days)
    n125 = sum(1 for t in add if t["pos_after"] == 125)
    n150 = sum(1 for t in add if t["pos_after"] == 150)
    wins = sum(1 for c in closed if c["ret"] > 0)
    return {
        "years": round(years, 1),
        "add_total": len(add),
        "add_per_year": round(len(add) / years, 1),
        "clear_total": len(clear),
        "clear_per_year": round(len(clear) / years, 2),
        "temp_sell_per_year": round(len(temp_sell) / years, 1),
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 0) if hold_days else 0,
        "hold_min": min(hold_days) if hold_days else 0,
        "hold_max": max(hold_days) if hold_days else 0,
        "by_year": by_year,
        "pct125": n125, "pct150": n150,
        "os_total": len(closed),
        "os_winrate": round(wins / len(closed) * 100, 1) if closed else 0.0,
        "os_avg_ret": round(sum(c["ret"] for c in closed) / len(closed), 2) if closed else 0.0,
        "first": df["date"].iloc[0].strftime("%Y-%m-%d"),
        "last": df["date"].iloc[-1].strftime("%Y-%m-%d"),
    }


def run(tr_path, px_path, start=START, end=None, delay_sell=DELAY_SELL, p=None):
    """完整回测入口：读数据(含 warm-up) -> 信号 -> 撮合 -> 净值 -> 指标。
    df 保留 START 前数据作指标预热；回测与净值核算从 start 起。
    p=None 用默认参数（红利低波）；p=make_params(...) 用于变体（如沪深300）。"""
    df_all = get_prices(tr_path, px_path, end=end)
    df_all = build_signals(df_all, p=p)
    trades, closed, positions, state, pos, legs, t0 = replay(df_all, t1=True, start=start,
                                                             delay_sell=delay_sell, p=p)
    df = df_all[df_all["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    ec = equity_curve(df, trades, positions, start=start, p=p)
    m = metrics(ec, trades)
    m["n_oversold"] = len(closed)
    return {"df": df, "df_all": df_all, "trades": trades, "closed": closed, "positions": positions,
            "ec": ec, "metrics": m, "os_stat": oversold_stats(closed),
            "overview": overview_stats(trades, closed, df),
            "state": state, "pos": pos, "legs": legs, "t0": t0}


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[sys.argv.index("--out-dir") + 1] if "--out-dir" in sys.argv else base
    start = sys.argv[sys.argv.index("--start") + 1] if "--start" in sys.argv else START
    r = run(os.path.join(base, "h20269_daily.csv"), os.path.join(base, "h30269_daily.csv"), start)
    m = r["metrics"]
    print(f"交易日 {len(r['df'])} | 区间 {r['df']['date'].iloc[0].date()} ~ {r['df']['date'].iloc[-1].date()}")
    print(f"策略: 总收益 {m['total']*100:+.1f}% | 年化 {m['ann']*100:.2f}% | 夏普 {m['sharpe']:.3f} | 最大回撤 {m['mdd']*100:.1f}%")
    print(f"买入持有: 总收益 {m['total_bh']*100:+.1f}% | 年化 {m['ann_bh']*100:.2f}% | 夏普 {m['sharpe_bh']:.3f} | 回撤 {m['mdd_bh']*100:.1f}%")
    print(f"交易 {m['n_trades']} 笔 | 总成本(费+滑点+利息) {m['fee_total']:.2f} 元 | 终值 {m['final_value']:,.0f} / BH {m['final_bh']:,.0f}")
    st = r["os_stat"]
    print(f"抄底闭环 {st['total']} 档 胜率 {st['winrate']}% ({st['wins']}盈/{st['losses']}亏) 平均 {st['avg_ret']}%")
    # 写产物
    import pandas as pd
    ec = r["ec"]
    pd.DataFrame({"date": ec["dates"], "strategy_nav": ec["strategy_nav"], "bh_nav": ec["bh_nav"],
                  "strategy_dd": ec["strategy_dd"], "bh_dd": ec["bh_dd"],
                  "pos_pct": ec["pos_pct"], "costs": ec["costs"]}).to_csv(
        os.path.join(out, "equity_curve_v77.csv"), index=False)
    pd.DataFrame(r["trades"]).to_csv(os.path.join(out, "trades_v77.csv"), index=False)
    json.dump({k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in m.items()},
              open(os.path.join(out, "metrics_v77.json"), "w"), indent=2, ensure_ascii=False)
    print(f"产物已写入 {out}/ (equity_curve_v77.csv / trades_v77.csv / metrics_v77.json)")
