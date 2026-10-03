"""黄金信号校验：防口径漂移的硬门禁。

在 CI 中作为一道硬校验，任何一项不过就 fail，避免「改了执行口径但没人发现」。
可本地独立运行：python3 engine/gold_check.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIG = os.path.join(ROOT, "data", "kline", "gold", "gold_signal.parquet")
META = os.path.join(ROOT, "data", "meta", "gold_signal.json")

# 与说明书 / 回测报告一致的约定值，改动需先重跑离线回测
EXPECT = {
    "fast": 16,
    "slow": 40,
    "atr_n": 10,
    "slope": 0.75,
    "base": 0.0,
    "target_atr_pct": 0.025,
    "thresh": 0.0,
}
# 历史最大回撤基准（次周开盘口径，2007-2026 伦敦金）
HIST_MDD = -0.1251
# 回撤超过此值则告警（数据或口径可能出问题）
MDD_ALERT = -0.15


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def main() -> int:
    for p in (SIG, META):
        if not os.path.exists(p):
            fail(f"缺少文件 {p}，请先运行 gold_signal.py")

    meta = json.load(open(META, encoding="utf-8"))
    params = meta["params"]
    latest = meta["latest"]
    perf = meta["backtest"]

    # 1) 参数必须是约定值
    for k, v in EXPECT.items():
        if params.get(k) != v:
            fail(f"参数 {k} 被改为 {params.get(k)}，约定值 {v}。"
                 f"改参数必须先重跑离线回测并更新说明书。")
    print(f"[1/5] 参数校验通过: {EXPECT}")

    sig = pd.read_parquet(SIG)
    sig["date"] = pd.to_datetime(sig["date"])
    v = sig.dropna(subset=["target_pos"]).tail(52)
    if v.empty:
        fail("gold_signal.parquet 无有效信号行")

    # 2) 仓位必须落在 [0, 1]
    lo, hi = float(v["target_pos"].min()), float(v["target_pos"].max())
    if lo < -1e-9 or hi > 1 + 1e-9:
        fail(f"目标仓位越界 [{lo:.4f}, {hi:.4f}]，应在 [0, 1]")
    print(f"[2/5] 近 52 周仓位区间 [{lo:.1%}, {hi:.1%}]，均值 {v['target_pos'].mean():.1%}")

    # 3) 复核最新一周 P = B × V
    last = v.iloc[-1]
    b_exp = float(np.clip(params["slope"] * last["strength"], 0, 1))
    v_exp = float(min(1.0, params["target_atr_pct"] / last["atr_pct"]))
    p_exp = b_exp * v_exp
    if abs(p_exp - float(last["target_pos"])) > 1e-9:
        fail(f"最新一周 P={last['target_pos']:.6f} 与 B×V={p_exp:.6f} 不符")
    print(f"[3/5] P = B × V 复核通过: {b_exp:.4f} × {v_exp:.4f} = {p_exp:.4f}")

    # 4) 指标公式复核：用原始 OHLC 重算 MA16 / MA40 / ATR10
    w = sig.dropna(subset=["target_pos"]).copy()
    if len(w) >= 40:
        ma_s = w["close"].rolling(params["slow"]).mean().iloc[-1]
        ma_f = w["close"].rolling(params["fast"]).mean().iloc[-1]
        if abs(ma_s - last["ma_slow"]) > 1e-6 or abs(ma_f - last["ma_fast"]) > 1e-6:
            fail(f"均线复核不一致: MA{sig.attrs.get('x', '')}"
                 f"期望({ma_f:.4f},{ma_s:.4f}) 实际({last['ma_fast']:.4f},{last['ma_slow']:.4f})")
        # ATR 用周线 high/low/close 重算
        h, l, c = (w["high"].to_numpy(float), w["low"].to_numpy(float),
                   w["close"].to_numpy(float))
        pc = np.r_[np.nan, c[:-1]]
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        atr = pd.Series(tr).rolling(params["atr_n"]).mean().iloc[-1]
        if abs(atr - last["atr"]) > 1e-6:
            fail(f"ATR 复核不一致: 期望 {atr:.4f} 实际 {last['atr']:.4f}")
        print(f"[4/5] 指标复核通过: MA{params['fast']}={ma_f:.2f} "
              f"MA{params['slow']}={ma_s:.2f} ATR{params['atr_n']}={atr:.2f}")

    # 5) 回撤不得显著偏离历史
    mdd = float(perf["最大回撤"])
    if mdd < MDD_ALERT:
        warn(f"回撤 {mdd:.2%} 超出历史 {HIST_MDD:.2%} 较多，请复核数据源与执行口径")
    print(f"[5/5] 回测: 年化 {perf['年化']:.2%} 夏普 {perf['夏普']} "
          f"最大回撤 {mdd:.2%} 平均仓位 {perf['平均仓位']:.1%}")

    print(f"\n最新信号 {latest['week']}: S={latest['strength']:+.3f} "
          f"B={latest['pos_trend']:.1%} V={latest['pos_vol']:.3f} "
          f"目标仓位={latest['target_pos']:.1%}")
    print("全部校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
