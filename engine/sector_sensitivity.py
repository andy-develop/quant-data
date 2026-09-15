#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 行业轮动 · 过拟合体检（P0-4，审计整改项 4）
1) 参数敏感性：TOP_N / SWAP_GAP / STAY_PCT / CB_EXCESS_DD / SL_STOP / MK_CAP80 / MK_CAP90 / 成本，
   各 ± 扰动报告 Sharpe / 总收益 / 最大回撤 / 交易数，找"参数平原"而非孤立最优点
2) Walk-forward：固定参数在 3 个不相交子区间上报告，检查跨区间一致性（不在测试段上调参）
3) 随机选股对照：RAND_N 次随机得分回测（同日程/门槛/留仓/换手/风控，仅因子替换随机数），
   报告真实夏普在随机分布中的分位与 p 值（单侧：P(随机 ≥ 真实)）
4) 邻域微扰（策略层审计 S-1）：对行业日收益加 σ=0.005~0.20 高斯噪声、每档多 seed，
   报告总收益/夏普/回撤 P5/中位数/P95——考察结果对输入微扰的稳定性
输出: data/sector-sensitivity.json（固定 seed 42，确定性可复现）
用法: python3 sector_sensitivity.py [--raw data/sector-week-....json.gz] [--quick]
"""
import json, os, sys
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sector_universe as U
import sector_engine as E
import sector_update as SU

SENS = {
    "TOP_N": [3, 4, 5],
    "SWAP_GAP": [0.10, 0.15, 0.20],
    "STAY_PCT": [0.50, 0.60, 0.70],
    "CB_EXCESS_DD": [0.05, 0.08, 0.12],
    "SL_STOP": [0.08, 0.12, 0.16],
    "MK_CAP80": [0.60, 0.70, 0.80],
    "MK_CAP90": [0.40, 0.50, 0.60],
    "SLIP_BPS": [2, 5, 10],
}
WF = [
    ("2018-2020", "2018-01-01", "2020-12-31"),
    ("2021-2023", "2021-01-01", "2023-12-29"),
    ("2024-2026", "2024-01-01", None),   # None = 数据末尾
]
RAND_N = 150
RNG_SEED = 42


def load_panel():
    if "--raw" in sys.argv:
        p = sys.argv[sys.argv.index("--raw") + 1]
        raw = SU.load_raw(p)
    else:
        raw, _, _ = SU.rebuild_base()
    raw, removed = SU.clean_intraday(raw)
    panel = E.build_panel(raw)
    return panel, E.compute_factors(panel), removed


def run_metrics(panel, fac, start=E.START, end=None):
    m = E.backtest(panel, fac, start=start, end=end)["metrics"]
    # compute_metrics 在 NAV≤0（如极端噪声致净值归零）时返回 {}，此时如实标 None 而非崩溃
    return {k: (round(m[k], 4) if m.get(k) is not None else None)
            for k in ("total", "sharpe", "mdd", "n_trades")}


def main():
    quick = "--quick" in sys.argv
    global RAND_N
    if quick:
        RAND_N = 30
    panel, fac, removed = load_panel()
    out = {"baseline": None, "sensitivity": [], "walk_forward": [], "random_control": {},
           "note": "固定 seed 42；随机对照仅替换得分（同日程/门槛/留仓/换手/风控）；"
                   "PBO / Deflated Sharpe 未实现（见交付说明披露）"}
    base = run_metrics(panel, fac)
    out["baseline"] = base
    print(f"基线: 总收益{base['total']*100:+.1f}% 夏普{base['sharpe']:.2f} 回撤{base['mdd']*100:.1f}% {base['n_trades']}笔")
    for name, values in SENS.items():
        orig = getattr(E, name)
        for v in values:
            if abs(v - orig) < 1e-12:
                continue
            setattr(E, name, v)
            try:
                m = run_metrics(panel, fac)
                row = {"param": name, "value": v, **m}
                out["sensitivity"].append(row)
                print(f"  {name}={v}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
            finally:
                setattr(E, name, orig)
    for label, s, e in WF:
        m = run_metrics(panel, fac, start=s, end=e)
        out["walk_forward"].append({"period": label, "start": s, "end": e or str(panel["dates"][-1].date()), **m})
        print(f"  WF {label}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    # 随机选股对照：仅替换 score（z 型随机），pooled/风控/换手逻辑不变
    rng = np.random.default_rng(RNG_SEED)
    sharpes = np.empty(RAND_N)
    totals = np.empty(RAND_N)
    score0 = fac["score"]
    pooled0 = fac["pooled"]
    for k in range(RAND_N):
        fac["score"] = rng.standard_normal(score0.shape) * np.where(pooled0, 1.0, np.nan)
        m = E.backtest(panel, fac)["metrics"]
        sharpes[k] = m["sharpe"]
        totals[k] = m["total"]
    fac["score"] = score0
    real = base["sharpe"]
    pct = float((sharpes < real).mean())
    out["random_control"] = {
        "n": RAND_N, "seed": RNG_SEED,
        "sharpe": {"mean": round(float(sharpes.mean()), 3), "p50": round(float(np.median(sharpes)), 3),
                   "p95": round(float(np.percentile(sharpes, 95)), 3), "max": round(float(sharpes.max()), 3)},
        "total_mean": round(float(totals.mean()), 4),
        "real_sharpe": real, "percentile": round(float(pct), 3),
        "p_value": round(float(1 - pct), 3),   # 单侧：P(随机 ≥ 真实)
    }
    print(f"  随机对照 n={RAND_N}: 随机夏普 mean {sharpes.mean():.3f} / p95 {np.percentile(sharpes,95):.3f} / max {sharpes.max():.3f}；"
          f"真实 {real:.3f} 分位 {pct:.1%} p={1-pct:.3f}")
    # 邻域微扰（策略层审计 S-1）：对原始行业日收益加高斯噪声，考察结果对输入微扰的稳定性。
    # 与随机对照的区别：随机对照替换整张得分表（因子完全失效），邻域仅扰动输入收益
    # （因子信息仍在，只是被噪声稀释）——直接回应"结果对微扰脆弱"指控。
    # σ=0.005~0.20 每档多 seed，报告总收益/夏普/回撤的 P5/中位数/P95。
    R0 = panel["R"].copy()
    n_seed = 3 if quick else 5
    sigmas = [0.005, 0.01, 0.02, 0.05, 0.10, 0.20]
    neigh = []
    for sig in sigmas:
        totals, sharpes, mdds = [], [], []
        for k in range(n_seed):
            rng = np.random.default_rng(RNG_SEED * 1000 + int(round(sig * 10000)) + k)
            panel["R"] = R0 + rng.normal(0.0, sig, R0.shape)
            m = E.backtest(panel, fac)["metrics"]
            totals.append(m.get("total")); sharpes.append(m.get("sharpe")); mdds.append(m.get("mdd"))
        panel["R"] = R0

        def stat(a):
            v = [x for x in a if x is not None]
            if not v:
                return None
            return {"p5": round(float(np.percentile(v, 5)), 4),
                    "p50": round(float(np.median(v)), 4),
                    "p95": round(float(np.percentile(v, 95)), 4)}
        neigh.append({"sigma": sig, "n_seed": n_seed,
                      "total": stat(totals), "sharpe": stat(sharpes), "mdd": stat(mdds)})
        tv = [x for x in totals if x is not None]
        if tv:
            print(f"  邻域 σ={sig}: 收益中位 {np.median(tv)*100:+.1f}% (P5..P95 "
                  f"{np.percentile(tv,5)*100:+.1f}..{np.percentile(tv,95)*100:+.1f}%)")
        else:
            print(f"  邻域 σ={sig}: 全部 seed 净值归零，metrics=None")
    out["neighborhood"] = neigh
    out["cleaned_bars"] = len(removed)
    path = os.path.join(BASE, "data", "sector-sensitivity.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {path}")


if __name__ == "__main__":
    main()
