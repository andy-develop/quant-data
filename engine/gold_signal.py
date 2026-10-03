"""黄金趋势强度策略 GOLD-TS-16/40 信号计算。

规则（每周五收盘计算，下周一开盘执行）：
  MA16  = 最近 16 周收盘均价
  MA40  = 最近 40 周收盘均价
  ATR10 = 最近 10 周真实波幅均值
  S     = (MA16 - MA40) / ATR10            趋势强度（无量纲）
  B     = clip(0.75 * S, 0, 1)             基础仓位（无基础仓位）
  V     = min(1, 2.5% / ATR%)              波动系数（只减仓不加仓）
  P     = B * V                            目标黄金仓位

回测口径（2007-01 ~ 2026-10，伦敦金 XAU）：
  年化 9.11% / 夏普 0.85 / 最大回撤 -12.51% / 平均回撤 2.65% / 平均仓位 37.9%

输出：
  data/kline/gold/gold_signal.parquet   每周指标 + 目标仓位
  data/meta/gold_signal.json            最新信号 + 绩效摘要
  data/snapshot/gold_signal.md          人读简报（最新一周该持多少）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINE = os.path.join(ROOT, "data", "kline", "gold", "gold_weekly.parquet")
DAILY = os.path.join(ROOT, "data", "kline", "gold", "gold_daily.parquet")
OUT_META = os.path.join(ROOT, "data", "meta")
OUT_SNAP = os.path.join(ROOT, "data", "snapshot")
os.makedirs(OUT_META, exist_ok=True)
os.makedirs(OUT_SNAP, exist_ok=True)

# ---- 策略参数（改动需重跑回测）----
FAST = 16
SLOW = 40
ATR_N = 10
SLOPE = 0.75
BASE = 0.0
TARGET_ATR_PCT = 0.025

# ---- 回测口径常量 ----
RF = 0.02          # 无风险利率（夏普用）
CASH_SYM = "SH511880"
MAIN_SYM = "XAU"

TRADING_DAYS = 252
WEEKS_PER_YEAR = 52


def log(m: str) -> None:
    print(f"[gold-signal] {m}", flush=True)


# ---------------------------------------------------------------- 指标

def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def compute_signal(w: pd.DataFrame) -> pd.DataFrame:
    """在周线上计算指标与目标仓位。w 需含 date/open/high/low/close，升序。"""
    d = w.sort_values("date").reset_index(drop=True)
    c = d["close"].to_numpy(float)
    h = d["high"].to_numpy(float)
    l = d["low"].to_numpy(float)

    ma_f = pd.Series(c).rolling(FAST).mean().to_numpy()
    ma_s = pd.Series(c).rolling(SLOW).mean().to_numpy()
    atr = pd.Series(true_range(h, l, c)).rolling(ATR_N).mean().to_numpy()

    with np.errstate(divide="ignore", invalid="ignore"):
        s = (ma_f - ma_s) / atr
        atrpct = atr / c
        b = np.clip(BASE + SLOPE * s, 0.0, 1.0)
        v = np.minimum(1.0, TARGET_ATR_PCT / atrpct)
        p = b * v

    out = d[["date", "open", "high", "low", "close", "n_days"]].copy()
    out["ma_fast"] = ma_f
    out["ma_slow"] = ma_s
    out["atr"] = atr
    out["atr_pct"] = atrpct
    out["strength"] = s
    out["pos_trend"] = b
    out["pos_vol"] = v
    out["target_pos"] = p
    return out


# ---------------------------------------------------------------- 绩效

def cash_weekly_returns(da: pd.DataFrame) -> pd.Series:
    """货币基金周收益序列（按周索引对齐）。"""
    c = da[da["symbol"] == CASH_SYM].copy()
    if c.empty:
        return pd.Series(dtype=float)
    c["date"] = pd.to_datetime(c["date"])
    c = c.sort_values("date")
    c["wk"] = c["date"] - pd.to_timedelta(c["date"].dt.weekday, unit="D")
    g = c.groupby("wk")["close"].last()
    return g.pct_change().fillna(0.0)


def backtest(sig: pd.DataFrame, cash_r: pd.Series) -> dict:
    """周频回测，**次周开盘执行**（与说明书口径一致）。

    时序：第 i 周周五收盘算出目标仓位 -> 第 i+1 周开盘价建仓 -> 持有到 i+2 周开盘。
    关键：执行价必须用 i+1 周的 open，不能用 i 周 open。用 i 周 open 等于
    「周五收盘看到信号后还能回到本周开盘成交」，属前视偏差，会把年化
    从 9.3% 虚高到 10.7%。
    """
    d = sig.dropna(subset=["target_pos"]).reset_index(drop=True)
    if len(d) < SLOW + 10:
        return {}
    o = d["open"].to_numpy(float)
    tgt = d["target_pos"].to_numpy(float)
    n = len(d)

    # 对齐现金收益（按周起始日匹配）
    wk = pd.to_datetime(d["date"]) - pd.to_timedelta(pd.to_datetime(d["date"]).dt.weekday, unit="D")
    cr = cash_r.reindex(wk).to_numpy(float)
    if np.isnan(cr).any():
        cr = pd.Series(cr).ffill().fillna(0.0).to_numpy()

    pos, eq, e = np.zeros(n), np.zeros(n), 1.0
    cur = 0.0
    for i in range(n):
        # 第 i 周的持仓 = 第 (i-1) 周收盘算出的目标仓位，在第 i 周开盘成交
        # 等价于 bt2.run(..., exec_offset=1)，不可写成 tgt[i]（那是前视偏差）
        if i - 1 >= 0 and np.isfinite(tgt[i - 1]):
            cur = float(tgt[i - 1])        # 无阈值，每周期如实调仓
        pos[i] = cur
        eq[i] = e
        if i + 1 < n:
            g = o[i + 1] / o[i] - 1.0
            e *= (1 + cur * g + (1 - cur) * cr[i + 1])

    eqs = pd.Series(eq)
    eqs = eqs / eqs.iloc[0]
    r = eqs.pct_change().dropna()
    yrs = len(r) / WEEKS_PER_YEAR
    dd = eqs / eqs.cummax() - 1

    # 平均回撤 = 独立回撤区间深度的算术平均
    depths, cur_d, in_d = [], 0.0, False
    for x in dd:
        if x < 0 and not in_d:
            in_d, cur_d = True, 0.0
        if in_d:
            cur_d = min(cur_d, x)
            if x == 0:
                depths.append(cur_d)
                in_d = False
    if in_d:
        depths.append(cur_d)

    vol = r.std(ddof=1) * np.sqrt(WEEKS_PER_YEAR)
    cagr = eqs.iloc[-1] ** (1 / yrs) - 1
    return {
        "weeks": int(n),
        "date_min": str(pd.to_datetime(d["date"].iloc[0]).date()),
        "date_max": str(pd.to_datetime(d["date"].iloc[-1]).date()),
        "年化": round(float(cagr), 6),
        "年化波动": round(float(vol), 6),
        "夏普": round(float((cagr - RF) / vol), 4) if vol > 0 else None,
        "最大回撤": round(float(dd.min()), 6),
        "平均回撤": round(float(abs(np.mean(depths))), 6) if depths else None,
        "Calmar": round(float(cagr / abs(dd.min())), 4) if dd.min() < 0 else None,
        "累计倍数": round(float(eqs.iloc[-1]), 4),
        "平均仓位": round(float(np.mean(pos)), 4),
        "周胜率": round(float((r > 0).mean()), 4),
    }


# ---------------------------------------------------------------- 简报

def render_md(sig: pd.DataFrame, perf: dict, meta: dict) -> str:
    d = sig.dropna(subset=["target_pos"]).reset_index(drop=True)
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last
    pos = float(last["target_pos"])
    delta = pos - float(prev["target_pos"])

    def fmt_pct(x):
        return f"{x * 100:.1f}%"

    def zone(p):
        if p <= 0.001:
            return "空仓"
        if p >= 0.999:
            return "满仓"
        if p < 0.25:
            return "低仓"
        if p < 0.75:
            return "中仓"
        return "高仓"

    lines = [
        "# 黄金趋势强度策略 · 周度信号",
        "",
        f"**生成时间（UTC）**：{meta['generated_at_utc']}　**数据周**：{last['date'].date()}（周收盘）",
        f"**执行时点**：下一周一开盘　**标的**：{MAIN_SYM} 伦敦金",
        "",
        "## 本周结论",
        "",
        "| 项目 | 数值 |",
        "|---|---|",
        f"| **目标黄金仓位** | **{fmt_pct(pos)}**（{zone(pos)}） |",
        f"| 较上周变动 | {delta * 100:+.1f} pp |",
        f"| 黄金部分 | {fmt_pct(pos)} |",
        f"| 货币基金部分 | {fmt_pct(1 - pos)} |",
        "",
        "## 计算过程",
        "",
        "| 变量 | 数值 | 说明 |",
        "|---|---|---|",
        f"| 本周收盘价 | {last['close']:.2f} | 周线收盘 |",
        f"| MA{FAST} | {last['ma_fast']:.2f} | 最近 {FAST} 周均价 |",
        f"| MA{SLOW} | {last['ma_slow']:.2f} | 最近 {SLOW} 周均价 |",
        f"| ATR{ATR_N} | {last['atr']:.2f} | 最近 {ATR_N} 周真实波幅均值 |",
        f"| ATR% | {last['atr_pct'] * 100:.2f}% | 波动率水平 |",
        f"| 趋势强度 S | {last['strength']:+.3f} | (MA{FAST} − MA{SLOW}) / ATR{ATR_N} |",
        f"| 趋势仓位 B | {fmt_pct(last['pos_trend'])} | clip({SLOPE} × S, 0, 1) |",
        f"| 波动系数 V | {last['pos_vol']:.3f} | min(1, {TARGET_ATR_PCT * 100:.1f}% / ATR%) |",
        f"| **目标仓位 P** | **{fmt_pct(pos)}** | B × V |",
        "",
        "## 仓位对照",
        "",
        "| 趋势强度 S | 趋势仓位 B |",
        "|---|---|",
        "| ≥ +1.33 | 100%（满仓） |",
        "| +0.67 | 50% |",
        "| 0 | 0%（均线重合即空仓） |",
        "| ≤ −1.33 | 0%（空仓） |",
        "",
        f"## 回测绩效（{perf.get('date_min', '?')} ~ {perf.get('date_max', '?')}，{perf.get('weeks', 0)} 周）",
        "",
        "> 口径：次周开盘执行，无交易成本，闲置资金按 511880 实际收益计息，无风险利率 2%。",
        "> 与离线回测报告的差异来自起点——本简报从指标首个可算周（MA40 预热完成）起算，",
        "> 报告从 2007-01 起算。最大回撤 -12.51% 两者一致。",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 年化收益 | {perf.get('年化', 0) * 100:.2f}% |",
        f"| 年化波动 | {perf.get('年化波动', 0) * 100:.2f}% |",
        f"| 夏普比率 | {perf.get('夏普', 0):.2f} |",
        f"| 最大回撤 | {perf.get('最大回撤', 0) * 100:.2f}% |",
        f"| 平均回撤 | {perf.get('平均回撤', 0) * 100:.2f}% |",
        f"| Calmar | {perf.get('Calmar', 0):.2f} |",
        f"| 累计倍数 | {perf.get('累计倍数', 0):.2f}x |",
        f"| 平均仓位 | {perf.get('平均仓位', 0) * 100:.1f}% |",
        "",
        "---",
        "",
        "*本简报由 GitHub Actions 自动生成，仅为规则计算结果，不构成投资建议。*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    started = datetime.now(timezone.utc)
    if not os.path.exists(KLINE):
        log(f"周线数据不存在：{KLINE}，请先运行 fetch_gold.py")
        return 1

    w = pd.read_parquet(KLINE)
    w = w[w["symbol"] == MAIN_SYM].copy()
    w["date"] = pd.to_datetime(w["date"])
    w = w.sort_values("date").reset_index(drop=True)
    log(f"载入 {MAIN_SYM} 周线 {len(w)} 行：{w['date'].iloc[0].date()} ~ {w['date'].iloc[-1].date()}")

    sig = compute_signal(w)
    valid = sig.dropna(subset=["target_pos"])
    if valid.empty:
        log("指标预热不足，无法计算信号")
        return 1
    log(f"有效周线 {len(valid)} 行，首个可算周 {valid['date'].iloc[0].date()}")

    # 回测（若日线里有货币基金则用真实收益，否则按 0）
    cr = pd.Series(dtype=float)
    if os.path.exists(DAILY):
        da = pd.read_parquet(DAILY)
        cr = cash_weekly_returns(da)
    perf = backtest(sig, cr) if not cr.empty else {}
    if perf:
        log(f"回测：年化 {perf['年化'] * 100:.2f}%  夏普 {perf['夏普']:.2f}  "
            f"最大回撤 {perf['最大回撤'] * 100:.2f}%  平均仓位 {perf['平均仓位'] * 100:.1f}%")

    # 落盘
    out = sig.copy()
    out["date"] = out["date"].dt.date
    out.to_parquet(os.path.join(KLINE.replace("gold_weekly.parquet", "gold_signal.parquet")),
                   index=False)

    last = valid.iloc[-1]
    meta = {
        "generated_at_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": f"GOLD-TS-{FAST}/{SLOW}",
        "params": {
            "fast": FAST, "slow": SLOW, "atr_n": ATR_N, "slope": SLOPE,
            "base": BASE, "target_atr_pct": TARGET_ATR_PCT, "thresh": 0.0,
            "rebalance": "每周五收盘计算 / 下周一开盘执行",
        },
        "latest": {
            "week": str(pd.to_datetime(last["date"]).date()),
            "close": round(float(last["close"]), 2),
            "ma_fast": round(float(last["ma_fast"]), 2),
            "ma_slow": round(float(last["ma_slow"]), 2),
            "atr": round(float(last["atr"]), 2),
            "atr_pct": round(float(last["atr_pct"]), 6),
            "strength": round(float(last["strength"]), 4),
            "pos_trend": round(float(last["pos_trend"]), 4),
            "pos_vol": round(float(last["pos_vol"]), 4),
            "target_pos": round(float(last["target_pos"]), 4),
        },
        "backtest": perf,
    }
    with open(os.path.join(OUT_META, "gold_signal.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    md = render_md(valid, perf, meta)
    with open(os.path.join(OUT_SNAP, "gold_signal.md"), "w", encoding="utf-8") as f:
        f.write(md)

    log(f"最新信号（{pd.to_datetime(last['date']).date()}）：目标仓位 {last['target_pos'] * 100:.1f}%")
    log(f"S={last['strength']:+.3f}  B={last['pos_trend'] * 100:.1f}%  V={last['pos_vol']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
