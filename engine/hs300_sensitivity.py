#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 择时 · 过拟合体检（v9.2 配套，独立于红利低波）
1) 基线：v9.2 现值（X_UP=15 / Y_DOWN=14 / HOLD_DAYS=120 / BEAR_CORE=0.4 / CORE_CONFIRM=10
         / FORCE_MIN_ABOVE_MA=20 / OS_MIN_COUNT_BEAR=3 / VAL_HALF_CAP=1.0）
2) 参数敏感性：X_UP / Y_DOWN / HOLD_DAYS / REBUY_DAYS / BEAR_CORE / CORE_CONFIRM
               / FORCE_MIN_ABOVE_MA / OS_MIN_COUNT_BEAR / VAL_HALF_CAP
3) Walk-forward：2018-2020 / 2021-2023 / 2024-2026 三段固定参数（不在测试段上调参）
数据源：优先 parquet（与 gen_payload 一致）；无则回退本地 JSON 归档
输出: data/hs300-sensitivity.json（确定性，可复现）
用法: python3 hs300_sensitivity.py
"""
import json, os, sys
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import hs300_update as H
sys.path.insert(0, os.path.join(BASE, "backtest"))
import engine as E

P = H.P
START = E.START
WF = [
    ("2018-2020", "2018-01-01", "2020-12-31"),
    ("2021-2023", "2021-01-01", "2023-12-29"),
    ("2024-2026", "2024-01-01", None),
]
SENS = {
    "X_UP": [10, 15, 20, 25],
    "Y_DOWN": [10, 12, 14, 16],
    "HOLD_DAYS": [60, 90, 120, 9999],
    "REBUY_DAYS": [45, 90, 180],
    "BEAR_CORE": [0.3, 0.4, 0.5, 0.6, 1.0],
    "CORE_CONFIRM": [0, 5, 10, 15],
    "FORCE_MIN_ABOVE_MA": [0, 10, 20, 30],
    "OS_MIN_COUNT_BEAR": [2, 3, 4],
    "VAL_HALF_CAP": [1.0, 1.25, 1.5],
}
TMP_TR = "/tmp/hs300-tr.csv"
TMP_PX = "/tmp/hs300-px.csv"


def load_df():
    """优先 parquet；否则本地归档重建。"""
    pq_tr = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "H00300.parquet")
    pq_px = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "000300.parquet")
    if os.path.exists(pq_tr) and os.path.exists(pq_px):
        tr = pd.read_parquet(pq_tr)[["date", "close"]]
        px = pd.read_parquet(pq_px)[["date", "close"]].rename(columns={"close": "px"})
        df = tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)
        return df
    out, base_day = H.rebuild_hl()
    if out is None:
        raise RuntimeError("无 H00300/000300 数据（parquet 或 data/ 归档），先跑 fetch_index / hs300_update")
    tr = {r["tradeDate"]: r["close"] for r in out["H00300"]}
    px = {r["tradeDate"]: r["close"] for r in out["000300"]}
    dates = sorted(set(tr) & set(px))
    df = pd.DataFrame({"date": pd.to_datetime(dates),
                       "close": [tr[d] for d in dates],
                       "px": [px[d] for d in dates]})
    return df.sort_values("date").reset_index(drop=True)


def export_csv(df):
    pd.DataFrame({"date": df["date"], "close": df["close"], "vol": float("nan")}).to_csv(TMP_TR, index=False)
    pd.DataFrame({"date": df["date"], "close": df["px"]}).to_csv(TMP_PX, index=False)


def run_metrics(start=START, end=None, **over):
    """基线 = v9.1 现值（H.P）；over 仅覆盖被扫描的单项。"""
    base = {k: getattr(H.P, k) for k in
            ("X_UP", "Y_DOWN", "HOLD_DAYS", "REBUY_DAYS", "BEAR_CORE", "BULL_CORE",
             "CORE_CONFIRM", "CORE_STEP", "MAX_POS", "FORCE_MIN_ABOVE_MA", "OS_MIN_COUNT_BEAR",
             "OB_FROM_CD", "VAL_HALF_CAP")}
    base.update({k: v for k, v in over.items() if v is not None})
    p = E.make_params(**base)
    r = E.run(TMP_TR, TMP_PX, start=start, end=end, p=p)
    m = r["metrics"]
    return {k: (round(float(m[k]), 4) if isinstance(m[k], (int, float)) and m[k] == m[k] else None)
            for k in ("total", "sharpe", "mdd", "n_trades")}, r


def main():
    df = load_df()
    export_csv(df)
    print(f"数据: {len(df)} 条 {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    out = {"baseline": None, "sensitivity": [], "walk_forward": [],
           "note": "v9.2 固定参数现算；VAL_HALF_CAP=1.0 为半力只回补纪律"}
    base, _ = run_metrics()
    out["baseline"] = base
    print(f"基线(v9.2): 总收益{base['total']*100:+.1f}% 夏普{base['sharpe']:.2f} 回撤{base['mdd']*100:.1f}% {base['n_trades']}笔")
    for name, values in SENS.items():
        orig = getattr(P, name)
        for v in values:
            if abs(v - orig) < 1e-12:
                continue
            m, _ = run_metrics(**{name: v})
            row = {"param": name, "value": v, **m}
            out["sensitivity"].append(row)
            print(f"  {name}={v}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    for label, s, e in WF:
        m, _ = run_metrics(start=s, end=e)
        out["walk_forward"].append({"period": label, "start": s, "end": e or str(df["date"].iloc[-1].date()), **m})
        print(f"  WF {label}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% 回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    path = os.path.join(BASE, "data", "hs300-sensitivity.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {path}")


if __name__ == "__main__":
    main()
