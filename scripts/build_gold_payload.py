#!/usr/bin/env python3
"""黄金趋势强度策略（GOLD-TS-16/40）门户数据段。

输入（由 .github/workflows/gold.yml 每周六维护）：
  data/kline/gold/gold_signal.parquet  周线指标 + 目标仓位（engine/gold_signal.py 产出）
  data/kline/gold/gold_daily.parquet   日线（含 SH511880 货币基金，用于闲置资金计息）
  data/meta/gold_signal.json           最新信号 + 绩效摘要
输出：
  data/payload/gold.json               portal/build_portal.py 注入「ETF择时 → 黄金趋势强度」页

口径与 engine/gold_signal.py 一致：每周五收盘算信号、次周开盘执行、无交易成本、
闲置资金按 SH511880 实际收益计息、无风险利率 2%。本脚本用同一套规则复算周频净值曲线
（图上用），并与 gold_signal.json 的「累计倍数」自校验（偏差 > 0.5% 只警告、不中止）。

用法：python3 scripts/build_gold_payload.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
SIG = os.path.join(ROOT, "data", "kline", "gold", "gold_signal.parquet")
DAILY = os.path.join(ROOT, "data", "kline", "gold", "gold_daily.parquet")
META = os.path.join(ROOT, "data", "meta", "gold_signal.json")
OUT = os.path.join(ROOT, "data", "payload", "gold.json")

RECENT_WEEKS = 12      # 页面「最近 N 周」表
NAV_TOL = 0.005        # 净值曲线 vs 绩效摘要的自校验容差

sys.path.insert(0, ENGINE)
import gold_signal as GS  # noqa: E402  口径单一来源（货币基金周收益）


def log(m: str) -> None:
    print(f"[gold-payload] {m}", flush=True)


def rnd(x, n: int = 4):
    """NaN / None → None，其余四舍五入，方便 JSON 直出。"""
    if x is None:
        return None
    v = float(x)
    if not np.isfinite(v):
        return None
    return round(v, n)


def rebuild_nav(sig: pd.DataFrame, cash_r: pd.Series):
    """复算周频净值/仓位曲线（与 GS.backtest 同规则）。

    第 i 周的持仓 = 第 (i-1) 周收盘算出的目标仓位，在第 i 周开盘成交；
    区间 [open_i, open_{i+1}] 的收益记在 nav[i+1] 上，空仓部分拿货币基金收益。
    """
    o = sig["open"].to_numpy(float)
    tgt = sig["target_pos"].to_numpy(float)
    dt = pd.to_datetime(sig["date"])
    wk = dt - pd.to_timedelta(dt.dt.weekday, unit="D")
    cr = cash_r.reindex(wk).to_numpy(float)
    if np.isnan(cr).any():
        cr = pd.Series(cr).ffill().fillna(0.0).to_numpy()

    n = len(sig)
    nav = np.ones(n)
    pos = np.zeros(n)
    for i in range(n):
        if i - 1 >= 0 and np.isfinite(tgt[i - 1]):
            pos[i] = float(tgt[i - 1])
        if i + 1 < n:
            g = o[i + 1] / o[i] - 1.0
            nav[i + 1] = nav[i] * (1.0 + pos[i] * g + (1.0 - pos[i]) * cr[i + 1])
    return nav, pos


def annual_returns(dates, nav, bh):
    """分年收益（首尾年份为不完整年，页面注明）。"""
    df = pd.DataFrame({"d": pd.to_datetime(dates), "nav": nav, "bh": bh})
    df["y"] = df["d"].dt.year
    out = []
    prev_nav, prev_bh = 1.0, 1.0
    for y, g in df.groupby("y"):
        n_end, b_end = float(g["nav"].iloc[-1]), float(g["bh"].iloc[-1])
        out.append({
            "year": int(y),
            "n": int(len(g)),
            "strat": rnd(n_end / prev_nav - 1.0, 4),
            "bh": rnd(b_end / prev_bh - 1.0, 4),
        })
        prev_nav, prev_bh = n_end, b_end
    return out


def build() -> int:
    if not os.path.exists(SIG):
        log(f"周线信号文件不存在：{SIG}（请先跑 .github/workflows/gold.yml）")
        return 1
    if not os.path.exists(META):
        log(f"信号摘要不存在：{META}")
        return 1

    with open(META, encoding="utf-8") as f:
        meta = json.load(f)

    sig = pd.read_parquet(SIG)
    sig["date"] = pd.to_datetime(sig["date"])
    sig = sig.sort_values("date").dropna(subset=["target_pos"]).reset_index(drop=True)
    if sig.empty:
        log("指标预热不足，无有效周线")
        return 1

    cash_r = pd.Series(dtype=float)
    if os.path.exists(DAILY):
        cash_r = GS.cash_weekly_returns(pd.read_parquet(DAILY))
    if cash_r.empty:
        log("警告：取不到 511880 货币基金收益，闲置资金按 0 计息")
    nav, pos = rebuild_nav(sig, cash_r)

    dates = sig["date"].dt.strftime("%Y-%m-%d").tolist()
    close = sig["close"].astype(float).to_numpy()
    bh = close / close[0]
    perf = meta.get("backtest") or {}
    rep = perf.get("累计倍数")
    if rep:
        got = float(nav[-1])
        if abs(got - rep) / float(rep) > NAV_TOL:
            log(f"警告：复算累计倍数 {got:.4f}x 与摘要 {rep:.4f}x 偏差 >{NAV_TOL*100:.1f}%")
        else:
            log(f"净值曲线自校验通过：{got:.4f}x ≈ 摘要 {rep:.4f}x")

    recent = []
    for i in range(max(0, len(sig) - RECENT_WEEKS), len(sig)):
        prev = float(sig["target_pos"].iloc[i - 1]) if i > 0 else None
        recent.append({
            "week": dates[i],
            "close": rnd(close[i], 2),
            "strength": rnd(sig["strength"].iloc[i], 3),
            "pos": rnd(sig["target_pos"].iloc[i], 4),
            "delta": rnd(float(sig["target_pos"].iloc[i]) - prev, 4) if prev is not None else None,
        })
    recent.reverse()  # 新 → 旧

    payload = {
        "generated_at": meta.get("generated_at_utc"),
        "generated_at_cst": pd.Timestamp(meta.get("generated_at_utc")).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if meta.get("generated_at_utc") else None,
        "strategy": meta.get("strategy") or "GOLD-TS-16/40",
        "params": meta.get("params") or {},
        "latest": meta.get("latest") or {},
        "prev": {"week": dates[-2], "pos": rnd(sig["target_pos"].iloc[-2], 4)} if len(sig) > 1 else None,
        "backtest": perf,
        "series": {
            "dates": dates,
            "close": [rnd(v, 2) for v in close],
            "ma_fast": [rnd(v, 2) for v in sig["ma_fast"]],
            "ma_slow": [rnd(v, 2) for v in sig["ma_slow"]],
            "pos": [rnd(v, 4) for v in pos],
            "nav": [rnd(v, 4) for v in nav],
            "bh": [rnd(v, 4) for v in bh],
        },
        "annual": annual_returns(dates, nav, bh),
        "recent": recent,
        "recent_weeks": RECENT_WEEKS,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(f"已写出 {os.path.relpath(OUT, ROOT)}（{os.path.getsize(OUT)} B，"
        f"{len(sig)} 周，{dates[0]} ~ {dates[-1]}，最新目标仓位 "
        f"{float(sig['target_pos'].iloc[-1]) * 100:.1f}%）")
    return 0


def main() -> int:
    return build()


if __name__ == "__main__":
    sys.exit(main())