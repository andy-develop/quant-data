#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过拟合体检（审计整改项 7）
1) 参数敏感性：关键参数 ± 扰动，报告 Sharpe / 总收益 / 最大回撤 / 交易数，找"参数平原"而非孤立最优点
2) Walk-forward：固定参数在 3 个不相交子区间上报告，检查跨区间一致性（不在测试段上调参）
输出: backtest/sensitivity.json + 终端表格
注意：完整 PBO / Deflated Sharpe（组合净化交叉验证）未实现，见交付说明披露。
"""
import os, sys, json, copy
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import engine as E

TR = os.path.join(BASE, "h20269_daily.csv")
PX = os.path.join(BASE, "h30269_daily.csv")

# (参数名, 基准值, [候选值]) —— 候选含基准，每个参数单独扰动
SENS = {
    "J_LOW": [0.5, 1, 2],
    "J_HIGH": [90, 95, 100],
    "Y_DOWN": [15, 20, 25],
    "X_UP": [15, 20, 25],
    "RSI_OS": [30, 35, 40],
    "HOLD_DAYS": [45, 60, 75],
    "SLIPPAGE_BPS": [2, 5, 10],
    "FIN_RATE": [0.05, 0.07, 0.09],
}

WF = [
    ("2016-2019", "2016-09-08", "2019-12-31"),
    ("2020-2022", "2020-01-02", "2022-12-30"),
    ("2023-2026", "2023-01-03", None),   # None = 数据末尾
]


def run_metrics(start=None, end=None):
    r = E.run(TR, PX, start=start or E.START, end=end)
    m = r["metrics"]
    return {"total": m["total"], "ann": m["ann"], "sharpe": m["sharpe"],
            "mdd": m["mdd"], "n_trades": m["n_trades"], "os": r["os_stat"]["total"]}


def main():
    out = {"baseline": None, "sensitivity": [], "walk_forward": []}
    # baseline
    base = run_metrics()
    out["baseline"] = {k: round(v, 4) if isinstance(v, float) else v for k, v in base.items()}
    print(f"基线: 总收益{base['total']*100:+.1f}% 夏普{base['sharpe']:.2f} 回撤{base['mdd']*100:.1f}% {base['n_trades']}笔")
    # sensitivity
    for name, values in SENS.items():
        orig = getattr(E, name)
        for v in values:
            if abs(v - orig) < 1e-12:
                continue
            setattr(E, name, v)
            try:
                m = run_metrics()
                out["sensitivity"].append({"param": name, "value": v, **{k: round(x, 4) if isinstance(x, float) else x for k, x in m.items()}})
                print(f"  {name}={v}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
            finally:
                setattr(E, name, orig)
    # walk-forward
    for label, s, e in WF:
        # 用 start 截断 + 数据末尾截断
        m = run_metrics(s, e)
        out["walk_forward"].append({"period": label, "start": s, "end": e or "2026-09-08",
                                    **{k: round(x, 4) if isinstance(x, float) else x for k, x in m.items()}})
        print(f"  WF {label}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    with open(os.path.join(BASE, "sensitivity.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {BASE}/sensitivity.json")


if __name__ == "__main__":
    main()
