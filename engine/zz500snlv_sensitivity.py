#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中证500低波（500SNLV）择时 · 过拟合体检（v9.0 配套）
1) 基线：v9.0 现值（见 ZZ500SNLV_PARAMS / gen_payload.py）
2) 参数敏感性：单参数扰动（其余固定为 v9.0）
3) Walk-forward：2018-2020 / 2021-2023 / 2024-2026 三段固定参数
数据源：parquet H20782 + 930782（与 gen_payload 一致）
输出: data/zz500snlv-sensitivity.json
用法: python3 zz500snlv_sensitivity.py
"""
import json, os, sys
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "backtest"))
import engine as E

# 与 scripts/gen_payload.ZZ500SNLV_PARAMS 保持同步
P = E.make_params(X_UP=25.0, Y_DOWN=18.0, HOLD_DAYS=120, REBUY_DAYS=180,
                  BEAR_CORE=0.75, CORE_CONFIRM=10, OB_FROM_CD=True,
                  FORCE_MIN_ABOVE_MA=20, OS_MIN_COUNT_BEAR=3, VAL_HALF_CAP=1.0)
START = E.START
WF = [
    ("2018-2020", "2018-01-01", "2020-12-31"),
    ("2021-2023", "2021-01-01", "2023-12-29"),
    ("2024-2026", "2024-01-01", None),
]
SENS = {
    "X_UP": [15, 20, 25, 30],
    "Y_DOWN": [14, 16, 18, 20],
    "HOLD_DAYS": [60, 90, 120, 180],
    "REBUY_DAYS": [90, 120, 180, 240],
    "BEAR_CORE": [0.5, 0.6, 0.75, 1.0],
    "CORE_CONFIRM": [0, 5, 10, 15],
    "FORCE_MIN_ABOVE_MA": [0, 10, 20, 30],
    "OS_MIN_COUNT_BEAR": [2, 3, 4],
    "VAL_HALF_CAP": [1.0, 1.25, 1.5],
    "OB_FROM_CD": [False, True],
}
TMP_TR = "/tmp/zz500snlv-tr.csv"
TMP_PX = "/tmp/zz500snlv-px.csv"


def load_df():
    pq_tr = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "H20782.parquet")
    pq_px = os.path.join(os.path.dirname(BASE), "data", "kline", "index", "930782.parquet")
    if not (os.path.exists(pq_tr) and os.path.exists(pq_px)):
        raise RuntimeError("无 H20782/930782 parquet，先跑 scripts/fetch_index.py")
    tr = pd.read_parquet(pq_tr)[["date", "close"]]
    px = pd.read_parquet(pq_px)[["date", "close"]].rename(columns={"close": "px"})
    return tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)


def export_csv(df):
    pd.DataFrame({"date": df["date"], "close": df["close"], "vol": float("nan")}).to_csv(TMP_TR, index=False)
    pd.DataFrame({"date": df["date"], "close": df["px"]}).to_csv(TMP_PX, index=False)


def run_metrics(start=START, end=None, **over):
    base = {k: getattr(P, k) for k in
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
           "note": "v9.0 中证500低波固定参数；相对红利低波默认独立校准"}
    base, _ = run_metrics()
    out["baseline"] = base
    print(f"基线(v9.0): 总收益{base['total']*100:+.1f}% 夏普{base['sharpe']:.2f} "
          f"回撤{base['mdd']*100:.1f}% {base['n_trades']}笔")
    for name, values in SENS.items():
        orig = getattr(P, name)
        for v in values:
            if isinstance(v, bool) or isinstance(orig, bool):
                if v is orig:
                    continue
            elif abs(float(v) - float(orig)) < 1e-12:
                continue
            m, _ = run_metrics(**{name: v})
            row = {"param": name, "value": v, **m}
            out["sensitivity"].append(row)
            print(f"  {name}={v}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% "
                  f"回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    for label, s, e in WF:
        m, _ = run_metrics(start=s, end=e)
        out["walk_forward"].append({"period": label, "start": s,
                                    "end": e or str(df["date"].iloc[-1].date()), **m})
        print(f"  WF {label}: 夏普{m['sharpe']:.2f} 收益{m['total']*100:+.1f}% "
              f"回撤{m['mdd']*100:.1f}% {m['n_trades']}笔")
    path = os.path.join(BASE, "data", "zz500snlv-sensitivity.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {path}")


if __name__ == "__main__":
    main()
