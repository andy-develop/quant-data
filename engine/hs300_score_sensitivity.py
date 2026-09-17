#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 五维打分 · 阈值过拟合体检（标准版 v1 / 震荡优化版 v2）

目标：在约 10 年窗口（与仓位机一致 START=2016-09-08）上扫描 stance 阈值，
用 walk-forward + 邻域平台度挑「合理区间」，避免单点最优过拟合。

仓位映射（与 stance 文案一致的防御型多空）：
  看多 → 1.0；看平 → mid_pos（默认 0.5）；看空 → 0.0
信号 T 日收盘打分，T+1 调仓；收益用 H00300 全收益；成本 = 万1 + 5bp 滑点。

用法:
  python3 engine/hs300_score_sensitivity.py
  python3 engine/hs300_score_sensitivity.py --rebuild   # 强制重算打分缓存
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import build_hs300_score as S  # noqa: E402

START = "2016-09-08"
FEE_RATE = 0.0001
SLIPPAGE = 0.0005  # 5bp
TRADING_DAYS = 252
INITIAL = 100_000.0

CACHE = os.path.join(BASE, "data", "hs300_score_features.parquet")
OUT = os.path.join(BASE, "data", "hs300-score-sensitivity.json")

# 阈值网格：围绕现默认值做邻域，忌过密避免「挑尖峰」
THRESH_V1 = [0.15, 0.20, 0.25, 0.30, 0.33, 0.35, 0.40, 0.45, 0.50]
THRESH_V2 = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.33]

WF = [
    ("2018-2020", "2018-01-01", "2020-12-31"),
    ("2021-2023", "2021-01-01", "2023-12-29"),
    ("2024-2026", "2024-01-01", None),
]

# 合理区间判定（反过拟合）
PLATEAU_MAX_DROP = 0.12       # 邻点夏普相对峰值跌幅
MIN_UTIL = 0.15               # 多+空占比下限；过低=伪半仓（阈值过大≈常驻 mid）
MAX_MID = 0.85                # 看平占比上限（与 MIN_UTIL 互补）


def load_tr_px() -> pd.DataFrame:
    pq_tr = os.path.join(REPO, "data", "kline", "index", "H00300.parquet")
    pq_px = os.path.join(REPO, "data", "kline", "index", "000300.parquet")
    tr = pd.read_parquet(pq_tr)[["date", "close"]].rename(columns={"close": "tr"})
    px = pd.read_parquet(pq_px)[["date", "close"]].rename(columns={"close": "px"})
    tr["date"] = pd.to_datetime(tr["date"])
    px["date"] = pd.to_datetime(px["date"])
    return tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)


def build_scored(rebuild: bool = False) -> pd.DataFrame:
    if os.path.exists(CACHE) and not rebuild:
        print(f"load score cache: {CACHE}")
        df = pd.read_parquet(CACHE)
        df["date"] = pd.to_datetime(df["date"])
        return df
    print("rebuild score features (index + option + zt)…")
    idx = S.load_index()
    opt = S.load_option()
    zt = S.compute_limit_up_ratio()
    raw = S.build_raw_features(idx, opt, zt)
    scored = S.score_frame(raw)
    keep = ["date", "close", "score", "score_v2"] + [f"d_{k}" for k in S.DIMS]
    out = scored[keep].copy()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    out.to_parquet(CACHE, index=False)
    print(f"  cached → {CACHE} bars={len(out)}")
    return out


def score_to_pos(score: np.ndarray, threshold: float, mid_pos: float = 0.5) -> np.ndarray:
    """向量化 stance → 仓位。threshold==0：仅正负号；0 分为 mid。"""
    th = float(threshold)
    pos = np.full(len(score), mid_pos, dtype=float)
    if th > 0:
        pos[score > th] = 1.0
        pos[score < -th] = 0.0
    else:
        pos[score > 0] = 1.0
        pos[score < 0] = 0.0
        # score==0 → mid_pos（与 _stance_from_score 一致）
    return pos


def backtest(
    dates: pd.DatetimeIndex,
    tr: np.ndarray,
    score: np.ndarray,
    threshold: float,
    start: str = START,
    end: str | None = None,
    mid_pos: float = 0.5,
) -> dict:
    """T 日打分 → T+1 仓位；日收益 = 昨仓位 × 今日全收益涨跌。"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else dates[-1]
    pos_sig = score_to_pos(score, threshold, mid_pos=mid_pos)
    # T+1：仓位延迟一日
    pos = np.roll(pos_sig, 1)
    pos[0] = mid_pos

    rets = np.zeros(len(tr))
    rets[1:] = tr[1:] / tr[:-1] - 1.0

    mask = (dates >= start_ts) & (dates <= end_ts)
    idx = np.where(mask)[0]
    if len(idx) < 10:
        return {"total": None, "ann": None, "sharpe": None, "mdd": None,
                "n_trades": 0, "time_in_mkt": None, "bull_pct": None,
                "bear_pct": None, "mid_pct": None}

    # 成本：仓位变动日按 |Δpos| * notional 计费+滑点（简化）
    dpos = np.diff(pos, prepend=pos[0])
    # 策略日收益（先不计成本，再扣成本率）
    strat_rets = pos * rets
    cost_rets = np.abs(dpos) * (FEE_RATE + SLIPPAGE)
    strat_rets = strat_rets - cost_rets

    r = strat_rets[idx]
    bh = rets[idx]
    # 跳过首日（无昨仓）
    if len(r) > 1:
        r = r[1:]
        bh = bh[1:]
        pos_seg = pos[idx][1:]
        pos_sig_seg = pos_sig[idx][1:]
    else:
        pos_seg = pos[idx]
        pos_sig_seg = pos_sig[idx]

    nav = np.cumprod(1.0 + r)
    bh_nav = np.cumprod(1.0 + bh)
    total = float(nav[-1] - 1.0)
    years = len(r) / TRADING_DAYS
    ann = float(nav[-1] ** (1 / years) - 1) if years > 0 and nav[-1] > 0 else 0.0
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(TRADING_DAYS)) if r.std(ddof=1) > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    mdd = float(((nav / peak) - 1.0).min())
    total_bh = float(bh_nav[-1] - 1.0)
    sharpe_bh = float(bh.mean() / bh.std(ddof=1) * math.sqrt(TRADING_DAYS)) if bh.std(ddof=1) > 0 else 0.0
    peak_bh = np.maximum.accumulate(bh_nav)
    mdd_bh = float(((bh_nav / peak_bh) - 1.0).min())

    # 交易次数：仓位发生实质变化
    n_trades = int(np.sum(np.abs(np.diff(pos_seg, prepend=pos_seg[0])) > 1e-9))
    bull = float(np.mean(pos_sig_seg >= 0.99))
    bear = float(np.mean(pos_sig_seg <= 0.01))
    mid = float(1.0 - bull - bear)
    tim = float(np.mean(pos_seg))

    return {
        "total": round(total, 4),
        "ann": round(ann, 4),
        "sharpe": round(sharpe, 4),
        "mdd": round(mdd, 4),
        "total_bh": round(total_bh, 4),
        "sharpe_bh": round(sharpe_bh, 4),
        "mdd_bh": round(mdd_bh, 4),
        "n_trades": n_trades,
        "time_in_mkt": round(tim, 4),
        "bull_pct": round(bull, 4),
        "bear_pct": round(bear, 4),
        "mid_pct": round(mid, 4),
    }


def plateau_range(rows: list[dict], key: str = "sharpe") -> dict:
    """在阈值扫描结果上找夏普平台区间（峰值邻域衰减不超过 PLATEAU_MAX_DROP）。"""
    if not rows:
        return {"best": None, "lo": None, "hi": None, "members": []}
    best = max(rows, key=lambda x: (x[key] is not None, x[key] or -999))
    peak = best[key]
    if peak is None:
        return {"best": None, "lo": None, "hi": None, "members": []}
    members = []
    for r in rows:
        if r[key] is None:
            continue
        if peak <= 0:
            ok = r[key] >= peak - PLATEAU_MAX_DROP
        else:
            ok = r[key] >= peak * (1.0 - PLATEAU_MAX_DROP) or (peak - r[key]) <= PLATEAU_MAX_DROP
        if ok:
            members.append(r["threshold"])
    return {
        "best": best["threshold"],
        "best_sharpe": peak,
        "lo": min(members) if members else best["threshold"],
        "hi": max(members) if members else best["threshold"],
        "members": members,
    }


def _excess(full: dict, wfs: list[dict]) -> dict:
    """相对买入持有的超额夏普（WF 各段市场本身可能很差，用相对值更公平）。"""
    xs = (full["sharpe"] or 0) - (full.get("sharpe_bh") or 0)
    wf_xs = [(w["sharpe"] or 0) - (w.get("sharpe_bh") or 0) for w in wfs]
    return {
        "xs_sharpe": round(xs, 4),
        "avg_wf_xs": round(float(np.mean(wf_xs)), 4) if wf_xs else None,
        "min_wf_xs": round(float(np.min(wf_xs)), 4) if wf_xs else None,
        "avg_wf_sharpe": round(float(np.mean([w["sharpe"] for w in wfs])), 4) if wfs else None,
        "min_wf_sharpe": round(float(np.min([w["sharpe"] for w in wfs])), 4) if wfs else None,
    }


def recommend(version: str, full_rows: list[dict], wf_by_th: dict) -> dict:
    """反过拟合推荐：排除伪半仓 → 相对 BH 超额 → WF 最差段 → 平台中位偏好。"""
    enriched = []
    for full in full_rows:
        th = full["threshold"]
        wfs = wf_by_th.get(th, [])
        util = (full.get("bull_pct") or 0) + (full.get("bear_pct") or 0)
        mid = full.get("mid_pct") or 0
        pseudo = util < MIN_UTIL or mid > MAX_MID
        ex = _excess(full, wfs)
        enriched.append({
            "threshold": th,
            "sharpe": full["sharpe"],
            "total": full["total"],
            "mdd": full["mdd"],
            "n_trades": full["n_trades"],
            "mid_pct": mid,
            "util": round(util, 4),
            "pseudo_mid": pseudo,
            **ex,
        })

    usable = [c for c in enriched if not c["pseudo_mid"]]
    pool = usable if usable else enriched  # 极端情况下仍给出平台

    # 平台：在可用集合内按夏普邻域
    plat = plateau_range(
        [{"threshold": c["threshold"], "sharpe": c["sharpe"]} for c in pool]
    )
    robust = [c for c in pool if c["threshold"] in set(plat["members"])]
    if not robust:
        robust = pool

    def rank(c):
        # 优先抬高 WF 最差段相对 BH；惩罚过少交易噪声
        trade_pen = -0.03 if (c["n_trades"] or 0) < 40 else 0.0
        return (
            (c["min_wf_xs"] or -9) + trade_pen,
            c["avg_wf_xs"] or -9,
            c["sharpe"] or -9,
        )

    robust.sort(key=rank, reverse=True)
    pick = robust[0]
    lo = min(c["threshold"] for c in robust)
    hi = max(c["threshold"] for c in robust)
    # 平台中位：避免贴着扫描边界的尖峰
    ths = sorted(c["threshold"] for c in robust)
    median_th = ths[len(ths) // 2]
    # 若首选与中位差距大，偏向中位（更不易过拟合）
    if abs(pick["threshold"] - median_th) > 0.051:
        med_row = next(c for c in robust if c["threshold"] == median_th)
        # 中位不显著差于首选时采用中位
        if (med_row["min_wf_xs"] or -9) >= (pick["min_wf_xs"] or -9) - 0.08:
            pick = med_row

    return {
        "version": version,
        "recommended_threshold": pick["threshold"],
        "reasonable_range": [lo, hi],
        "reason": (
            f"排除伪半仓(util<{MIN_UTIL:.0%})后，按 WF 相对买入持有最差段优选；"
            f"建议 {pick['threshold']}（夏普 {pick['sharpe']:.2f}，"
            f"util={pick['util']:.0%}，WF超额均/最差 "
            f"{pick['avg_wf_xs']:.2f}/{pick['min_wf_xs']:.2f}）；"
            f"合理区间 [{lo}, {hi}]"
        ),
        "candidates": robust,
        "plateau": plat,
        "picked": pick,
        "rejected_pseudo_mid": [c["threshold"] for c in enriched if c["pseudo_mid"]],
    }


def run_version(
    name: str,
    score_col: str,
    thresholds: list[float],
    dates,
    tr,
    scored: pd.DataFrame,
    mid_pos: float = 0.5,
) -> dict:
    score = scored[score_col].astype(float).values
    # 对齐 NaN → 0（冷启动）
    score = np.nan_to_num(score, nan=0.0)

    full_rows = []
    print(f"\n=== {name} 全样本 {START}→ 阈值扫描 (mid_pos={mid_pos}) ===")
    for th in thresholds:
        m = backtest(dates, tr, score, th, start=START, end=None, mid_pos=mid_pos)
        row = {"threshold": th, **m}
        full_rows.append(row)
        print(
            f"  th={th:.2f}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% "
            f"回撤{m['mdd']*100:.1f}% 交易{m['n_trades']} "
            f"多/平/空={m['bull_pct']:.0%}/{m['mid_pct']:.0%}/{m['bear_pct']:.0%} "
            f"| BH夏普{m['sharpe_bh']:.2f} 回撤{m['mdd_bh']*100:.1f}%"
        )

    wf_by_th: dict[float, list] = {th: [] for th in thresholds}
    print(f"\n=== {name} Walk-forward ===")
    for th in thresholds:
        for label, s, e in WF:
            m = backtest(dates, tr, score, th, start=s, end=e, mid_pos=mid_pos)
            wf_by_th[th].append({"period": label, "start": s, "end": e, **m})
        # 只打印平台相关与默认附近
        wfs = wf_by_th[th]
        parts = " | ".join(f"{w['period']}夏普{w['sharpe']:.2f}" for w in wfs)
        print(f"  th={th:.2f}: {parts}")

    rec = recommend(name, full_rows, wf_by_th)
    print(f"\n>>> {name} 建议阈值={rec['recommended_threshold']} "
          f"合理区间={rec['reasonable_range']} | {rec['reason']}")
    return {
        "full": full_rows,
        "walk_forward": [
            {"threshold": th, "periods": wf_by_th[th]} for th in thresholds
        ],
        "recommendation": rec,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--mid-pos", type=float, default=0.5,
                    help="看平仓位，默认 0.5")
    args = ap.parse_args()

    prices = load_tr_px()
    scored = build_scored(rebuild=args.rebuild)
    df = prices.merge(scored[["date", "score", "score_v2"]], on="date", how="inner")
    df = df.sort_values("date").reset_index(drop=True)
    print(f"数据: {len(df)} 条 {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    print(f"回测起点: {START}（约10年，与 hs300 仓位机对齐）")

    dates = pd.DatetimeIndex(df["date"])
    tr = df["tr"].astype(float).values

    out = {
        "start": START,
        "end": str(df["date"].iloc[-1].date()),
        "mid_pos": args.mid_pos,
        "cost": {"fee_rate": FEE_RATE, "slippage": SLIPPAGE},
        "note": (
            "仓位: 多=1 / 平=mid_pos / 空=0；T+1 调仓；H00300 全收益；"
            "合理区间=夏普平台 ∩ WF三段不崩；推荐偏稳健而非全样本尖峰。"
        ),
        "v1": run_version("v1标准版", "score", THRESH_V1, dates, tr, df, args.mid_pos),
        "v2": run_version("v2震荡优化版", "score_v2", THRESH_V2, dates, tr, df, args.mid_pos),
        "current_defaults": {
            "v1": S.SIGNAL_SPECS["v1"]["threshold"],
            "v2": S.SIGNAL_SPECS["v2"]["threshold"],
        },
    }

    # 对照：当前默认
    for ver, col, th in (
        ("v1", "score", S.SIGNAL_SPECS["v1"]["threshold"]),
        ("v2", "score_v2", S.SIGNAL_SPECS["v2"]["threshold"]),
    ):
        m = backtest(dates, tr, np.nan_to_num(df[col].values, nan=0.0), th,
                     start=START, mid_pos=args.mid_pos)
        out.setdefault("baseline", {})[ver] = {"threshold": th, **m}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUT}")

    print("\n========== 结论 ==========")
    for ver in ("v1", "v2"):
        r = out[ver]["recommendation"]
        cur = out["current_defaults"][ver]
        print(
            f"{ver}: 当前默认={cur} → 建议={r['recommended_threshold']} "
            f"合理区间 {r['reasonable_range'][0]}~{r['reasonable_range'][1]}"
        )


if __name__ == "__main__":
    main()
