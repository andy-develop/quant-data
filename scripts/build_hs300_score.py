#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 ETF 择时 · 五维综合打分 [-1, +1]。

维度与入选指标（等权合成维度分，再等权得综合分）：
  价格  20日价格乖离率、20日布林带(%B)
  量能  20日换手率乖离率、60日换手率乖离率
  趋势  20日ADX（带方向）、20日创新高天数占比
  波动  期权隐含波动率、60日换手率波动
  拥挤  涨停占比5日均值、期权持仓量PCR均值

信号版本：
  v1 标准版：五维 10 指标，阈值 ±0.33（合理区间约 0.25~0.35；擅长大趋势）
  v2 震荡优化版：剔除量/价 4 因子，仅趋势+波动+拥挤 6 因子，阈值 ±0.20
      （合理区间约 0.15~0.25；胜率更高、更适用震荡市；忌阈值 0 过拟合切换）

标准化：各原始指标在滚动 756 交易日（约 3 年）窗口内做百分位 → 映射到 [-1,+1]
  score = 2 * percentile_rank - 1
冷启动：有效历史 < 60 交易日的指标当日强制记 0，并打 cold_start flag（避免 2 点打出 ±1）
方向（正分=偏多 / 负分=偏空）：
  价格乖离、布林%B、换手乖离、换手波动、涨停占比 → 取反（过热/拥挤偏空）
  ADX带方向、创新高占比、IV（恐慌）、PCR（恐慌） → 正向（趋势多 / 恐慌逆向偏多）

数据：
  指数 000300 OHLCV（volume 字段按成交额口径，作换手代理）
  股票 raw K 线 → 涨停占比（主板≥9.5% / 创业科创≥19.5%）
  期权日表 data/kline/option/hs300_option_daily.parquet（ATM IV + OI PCR）
  期权历史不足时：IV 回退 20 日已实现波动年化×100；PCR 缺失日该指标不参与均值

输出：data/payload/hs300_score.json

用法: python3 scripts/build_hs300_score.py
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

ROLL = 756
MIN_HISTORY = 60  # 百分位冷启动门槛：不足则记 0，不参与噪声极端分
PAYLOAD_KEEP_YEARS = int(os.environ.get("SCORE_KEEP_YEARS", "5"))
OPT_PATH = os.path.join(C.KDIR, "option", "hs300_option_daily.parquet")
OUT = os.path.join(C.PAYLOAD_DIR, "hs300_score.json")

DIMS = {
    "price": {
        "name": "价格",
        "indicators": ["bias20", "boll_pctb"],
    },
    "volume": {
        "name": "量能",
        "indicators": ["turn_bias20", "turn_bias60"],
    },
    "trend": {
        "name": "趋势",
        "indicators": ["adx_signed", "nh_ratio20"],
    },
    "volatility": {
        "name": "波动",
        "indicators": ["opt_iv", "turn_vol60"],
    },
    "crowd": {
        "name": "拥挤",
        "indicators": ["zt_ratio5", "oi_pcr"],
    },
}

# 信号版本：阈值经 2016-09-08 起约10年 H00300 回测 + WF 体检
# （见 engine/hs300_score_sensitivity.py）；取平台中段，避免伪半仓尖峰
SIGNAL_SPECS = {
    "v1": {
        "id": "v1",
        "name": "标准版",
        "title": "信号版本1（标准版）",
        "desc": (
            "使用全部10个指标。更擅长捕捉大趋势行情（如2014-2015年、2024年9月）。"
            "但在震荡市中表现相对较差，属于「震荡行情小亏，趋势行情多赚」的典型特征。"
            "阈值 ±0.33（合理区间 0.25~0.35；≥0.40 易退化成常驻看平）。"
        ),
        "dims": ["price", "volume", "trend", "volatility", "crowd"],
        "threshold": 0.33,
        "threshold_range": [0.25, 0.35],
        "score_col": "score",
    },
    "v2": {
        "id": "v2",
        "name": "震荡优化版",
        "title": "信号版本2（震荡优化版）",
        "desc": (
            "剔除量、价维度的4个因子，仅保留趋势、波动、拥挤维度的6个因子，阈值 ±0.20。"
            "提高了胜率、降低了赔率，收益稳定性更好，更适用于震荡市。"
            "合理区间 0.15~0.25；阈值 0（按正负号）切换过频，十年回测偏弱。"
        ),
        "dims": ["trend", "volatility", "crowd"],
        "threshold": 0.20,
        "threshold_range": [0.15, 0.25],
        "score_col": "score_v2",
    },
}

# 原始高值 → 打分前是否取反（True=过热/拥挤偏空）
FLIP = {
    "bias20": True,
    "boll_pctb": True,
    "turn_bias20": True,
    "turn_bias60": True,
    "adx_signed": False,
    "nh_ratio20": False,
    "opt_iv": False,       # 高 IV=恐慌 → 逆向偏多
    "turn_vol60": True,
    "zt_ratio5": True,     # 涨停多=拥挤偏空
    "oi_pcr": False,       # 高 PCR=恐慌 → 逆向偏多
}

IND_META = {
    "bias20": {"label": "20日价格乖离率", "unit": "%"},
    "boll_pctb": {"label": "20日布林带%B", "unit": ""},
    "turn_bias20": {"label": "20日换手率乖离率", "unit": "%"},
    "turn_bias60": {"label": "60日换手率乖离率", "unit": "%"},
    "adx_signed": {"label": "20日ADX（带方向）", "unit": ""},
    "nh_ratio20": {"label": "20日创新高天数占比", "unit": "%"},
    "opt_iv": {"label": "期权隐含波动率", "unit": "%"},
    "turn_vol60": {"label": "60日换手率波动", "unit": "%"},
    "zt_ratio5": {"label": "涨停占比5日均值", "unit": "%"},
    "oi_pcr": {"label": "期权持仓量PCR", "unit": ""},
}


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _std(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).std()


def _percentile_score(
    raw: pd.Series,
    window: int = ROLL,
    flip: bool = False,
    min_history: int = MIN_HISTORY,
) -> pd.Series:
    """滚动百分位 → [-1, +1]；可选方向翻转。

    在非空子序列上计算，再对齐回原索引（适配期权日表冷启动的稀疏序列）。
    有效历史 < min_history 时明确记 0（不做百分位），避免 2～3 个点打出 ±1。
    """
    out = pd.Series(np.nan, index=raw.index, dtype=float)
    mask = raw.notna()
    if mask.sum() < 1:
        return out
    sub = raw[mask].astype(float)
    nn = len(sub)

    # 整段历史不足门槛 → 有值日一律记 0
    if nn < min_history:
        out.loc[sub.index] = 0.0
        return out

    use_window = window if nn >= max(min_history, window // 4) else max(nn, min_history)

    def _rank(x):
        if len(x) < 1 or np.isnan(x[-1]):
            return np.nan
        v = x[~np.isnan(x)]
        if len(v) < min_history:
            return np.nan
        return float(np.mean(v <= v[-1]))

    pct = sub.rolling(use_window, min_periods=min_history).apply(_rank, raw=True)
    score = 2.0 * pct - 1.0
    if flip:
        score = -score
    # 滚动窗口未满 min_history 的早期点：fillna(0)，与整段冷启动口径一致
    out.loc[score.index] = score.clip(-1, 1).fillna(0.0)
    return out


def _cold_start_mask(raw: pd.Series, min_history: int = MIN_HISTORY) -> pd.Series:
    """有值且累计有效历史 < min_history → True（冷启动）。"""
    hist = raw.notna().cumsum()
    return raw.notna() & (hist < min_history)


def _adx_signed(df: pd.DataFrame, n: int = 20) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    up = high - prev_high
    dn = prev_low - low
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = pd.Series(tr, index=df.index).ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    # 带方向：ADX * sign(+DI - -DI) / 100 ∈ 约 [-1,1]
    signed = adx / 100.0 * np.sign(plus_di - minus_di)
    return signed.rename("adx_signed")


def _nh_ratio(high: pd.Series, n: int = 20) -> pd.Series:
    """近 n 日中， innovate n 日新高的天数占比。"""
    roll_max = high.rolling(n, min_periods=n).max()
    is_nh = (high >= roll_max).astype(float)
    return is_nh.rolling(n, min_periods=n).mean()


def _realized_vol_pct(close: pd.Series, n: int = 20) -> pd.Series:
    ret = np.log(close / close.shift(1))
    return ret.rolling(n, min_periods=n).std() * math.sqrt(252) * 100.0


def load_index() -> pd.DataFrame:
    path = os.path.join(C.INDEX_DIR, "000300.parquet")
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    # volume 字段实为成交额口径（与天气模块一致）
    df["amount"] = df["volume"].astype(float)
    df["turn"] = df["amount"]  # 代理换手
    return df


def load_option() -> pd.DataFrame:
    if not os.path.exists(OPT_PATH):
        return pd.DataFrame(columns=["date", "atm_iv", "oi_pcr"])
    df = pd.read_parquet(OPT_PATH)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def compute_limit_up_ratio() -> pd.DataFrame:
    """全市场涨停占比日序列（DuckDB）。含年分片 + 日增量。"""
    import duckdb

    files = []
    for f in sorted(os.listdir(C.STOCK_DIR)):
        if f.startswith("raw_") and f.endswith(".parquet"):
            files.append(os.path.join(C.STOCK_DIR, f).replace("\\", "/"))
    if not files:
        return pd.DataFrame(columns=["date", "zt_ratio"])

    # 创业板 0.3xxxxx / 科创 1.688xxx → 20% 板；其余 10% 板
    globs = ",".join(f"'{p}'" for p in files)
    q = f"""
    WITH raw AS (
      SELECT code,
             CAST(date AS DATE) AS d,
             close,
             lag(close) OVER (PARTITION BY code ORDER BY date) AS prev
      FROM read_parquet([{globs}])
    ),
    tagged AS (
      SELECT d, code, close, prev,
             CASE
               WHEN starts_with(code, '0.3') OR starts_with(code, '1.688') THEN 0.195
               ELSE 0.095
             END AS thr
      FROM raw
      WHERE prev IS NOT NULL AND prev > 0 AND close > 0
    ),
    day AS (
      SELECT d AS date,
             count(*)::DOUBLE AS n,
             sum(CASE WHEN (close/prev - 1) >= thr THEN 1 ELSE 0 END)::DOUBLE AS zt
      FROM tagged
      GROUP BY 1
    )
    SELECT date, zt / n AS zt_ratio FROM day WHERE n >= 1000 ORDER BY 1
    """
    con = duckdb.connect()
    out = con.execute(q).df()
    out["date"] = pd.to_datetime(out["date"])
    return out


def build_raw_features(idx: pd.DataFrame, opt: pd.DataFrame, zt: pd.DataFrame) -> pd.DataFrame:
    df = idx.copy()
    close = df["close"]
    ma20 = _sma(close, 20)
    std20 = _std(close, 20)
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20

    df["bias20"] = (close / ma20 - 1.0) * 100.0
    df["boll_pctb"] = (close - lower) / (upper - lower).replace(0, np.nan)

    turn = df["turn"].astype(float)
    df["turn_bias20"] = (turn / _sma(turn, 20) - 1.0) * 100.0
    df["turn_bias60"] = (turn / _sma(turn, 60) - 1.0) * 100.0
    df["turn_vol60"] = (_std(turn / _sma(turn, 60), 60) * 100.0)

    df["adx_signed"] = _adx_signed(df, 20)
    df["nh_ratio20"] = _nh_ratio(df["high"], 20) * 100.0  # 存为 %

    # 期权 + IV 回退
    hv20 = _realized_vol_pct(close, 20)
    if len(opt):
        o = opt[["date", "atm_iv", "oi_pcr"]].copy()
        df = df.merge(o, on="date", how="left")
    else:
        df["atm_iv"] = np.nan
        df["oi_pcr"] = np.nan
    df["opt_iv"] = df["atm_iv"].where(df["atm_iv"].notna(), hv20)
    df["opt_iv_is_proxy"] = df["atm_iv"].isna() & df["opt_iv"].notna()

    if len(zt):
        df = df.merge(zt, on="date", how="left")
    else:
        df["zt_ratio"] = np.nan
    df["zt_ratio5"] = _sma(df["zt_ratio"], 5) * 100.0  # %

    # PCR：持仓量 PCR 的滚动均值（min_periods=1，首日即可用）
    df["oi_pcr_raw"] = df["oi_pcr"]
    df["oi_pcr"] = df["oi_pcr_raw"].rolling(5, min_periods=1).mean()
    return df


def score_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df[["date", "close"]].copy()
    for key, flip in FLIP.items():
        out[f"raw_{key}"] = df[key]
        out[f"s_{key}"] = _percentile_score(df[key], ROLL, flip=flip)
        out[f"cold_{key}"] = _cold_start_mask(df[key], MIN_HISTORY)

    for dim, meta in DIMS.items():
        cols = [f"s_{k}" for k in meta["indicators"]]
        out[f"d_{dim}"] = out[cols].mean(axis=1, skipna=True)

    # v1：五维等权；v2：仅趋势/波动/拥挤三维等权
    out["score"] = out[[f"d_{k}" for k in SIGNAL_SPECS["v1"]["dims"]]].mean(axis=1, skipna=True)
    out["score_v2"] = out[[f"d_{k}" for k in SIGNAL_SPECS["v2"]["dims"]]].mean(axis=1, skipna=True)
    out["opt_iv_is_proxy"] = df["opt_iv_is_proxy"]
    return out


def _f(x, nd=4):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _stance_from_score(score: float, threshold: float) -> tuple[str, str, str, str]:
    """返回 (stance, stance_zh, advice, band)。

    threshold>0：死区 [-th, +th] 为看平；threshold==0：仅按正负号分多空。
    """
    th = float(threshold)
    if score > th:
        return "bullish", "看多", "市场偏强，考虑积极或持有", "bull"
    if score < -th if th > 0 else score < 0:
        return "bearish", "看空", "市场偏弱，考虑减仓或防御", "bear"
    return "neutral", "看平", "多空均衡，视为震荡，观望或高抛低吸", "mid"


def _signal_snapshot(row: pd.Series, spec: dict, all_indicators: list, all_dims: list) -> dict:
    """按版本裁剪维度/指标，并打 stance。"""
    dim_keys = set(spec["dims"])
    ind_keys = {k for d in spec["dims"] for k in DIMS[d]["indicators"]}
    score = float(row[spec["score_col"]])
    stance, stance_zh, advice, band = _stance_from_score(score, spec["threshold"])
    cold_keys = [it["key"] for it in all_indicators if it["key"] in ind_keys and it.get("cold_start")]
    return {
        "id": spec["id"],
        "name": spec["name"],
        "title": spec["title"],
        "desc": spec["desc"],
        "stance_threshold": spec["threshold"],
        "dims_used": list(spec["dims"]),
        "n_indicators": len(ind_keys),
        "score": _f(score, 4),
        "stance": stance,
        "stance_zh": stance_zh,
        "advice": advice,
        "band": band,
        "dims": [d for d in all_dims if d["key"] in dim_keys],
        "indicators": [it for it in all_indicators if it["key"] in ind_keys],
        "cold_start_indicators": cold_keys,
    }


def latest_payload(scored: pd.DataFrame, opt_days: int = 0) -> dict:
    row = scored.dropna(subset=["score"]).iloc[-1]
    indicators = []
    cold_keys = []
    for key, meta in IND_META.items():
        raw = row.get(f"raw_{key}")
        s = row.get(f"s_{key}")
        cold = bool(row.get(f"cold_{key}", False))
        if cold:
            cold_keys.append(key)
        indicators.append({
            "key": key,
            "label": meta["label"],
            "unit": meta["unit"],
            "raw": _f(raw, 4),
            "score": _f(s, 4),
            "flip": FLIP[key],
            "dim": next(d for d, m in DIMS.items() if key in m["indicators"]),
            "cold_start": cold,
        })

    dims = []
    for dim, meta in DIMS.items():
        dims.append({
            "key": dim,
            "name": meta["name"],
            "score": _f(row.get(f"d_{dim}"), 4),
            "indicators": meta["indicators"],
        })

    signals = {
        sid: _signal_snapshot(row, spec, indicators, dims)
        for sid, spec in SIGNAL_SPECS.items()
    }
    # 兼容旧前端：snapshot = v1
    v1 = signals["v1"]
    stance, stance_zh, advice, _band = (
        v1["stance"], v1["stance_zh"], v1["advice"], v1["band"]
    )

    # 历史序列（截断）
    keep_from = pd.Timestamp(row["date"]) - pd.DateOffset(years=PAYLOAD_KEEP_YEARS)
    hist = scored[scored["date"] >= keep_from].copy()
    series = []
    for _, r in hist.iterrows():
        if pd.isna(r["score"]):
            continue
        series.append({
            "d": r["date"].strftime("%Y-%m-%d"),
            "s": _f(r["score"], 4),
            "s2": _f(r.get("score_v2"), 4),
            "price": _f(r.get("d_price"), 4),
            "volume": _f(r.get("d_volume"), 4),
            "trend": _f(r.get("d_trend"), 4),
            "volatility": _f(r.get("d_volatility"), 4),
            "crowd": _f(r.get("d_crowd"), 4),
            "px": _f(r["close"], 2),
        })

    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": row["date"].strftime("%Y-%m-%d"),
        "version": "v1.3",
        "default_signal": "v1",
        "method": {
            "range": [-1, 1],
            "window": ROLL,
            "min_history": MIN_HISTORY,
            "agg": "equal_weight",
            "stance_threshold": SIGNAL_SPECS["v1"]["threshold"],
            "signals": {
                sid: {
                    "name": sp["name"],
                    "title": sp["title"],
                    "dims": sp["dims"],
                    "threshold": sp["threshold"],
                    "threshold_range": sp.get("threshold_range"),
                    "n_indicators": sum(len(DIMS[d]["indicators"]) for d in sp["dims"]),
                }
                for sid, sp in SIGNAL_SPECS.items()
            },
            "note": ("滚动3年百分位映射到[-1,+1]；价格/量能/换手波动/涨停取反；"
                     "趋势与恐慌类(IV/PCR)正向。换手用指数成交额代理；"
                     "IV缺历史时用20日已实现波动回退；"
                     f"有效历史<{MIN_HISTORY}交易日的指标强制记0并标记cold_start。"
                     "v1阈值±0.33（区间0.25~0.35）；v2去量价、阈值±0.20（区间0.15~0.25）。"),
        },
        "signals": signals,
        "snapshot": {
            "score": v1["score"],
            "stance": stance,
            "stance_zh": stance_zh,
            "advice": advice,
            "close": _f(row["close"], 2),
            "dims": dims,
            "indicators": indicators,
            "cold_start_indicators": cold_keys,
            "opt_iv_is_proxy": bool(row.get("opt_iv_is_proxy", False)),
            "option_history_days": int(opt_days),
            # 冗余字段，便于旧逻辑读取 v2
            "score_v2": signals["v2"]["score"],
            "stance_v2": signals["v2"]["stance"],
            "stance_zh_v2": signals["v2"]["stance_zh"],
            "advice_v2": signals["v2"]["advice"],
        },
        "series": series,
    }


def build() -> dict:
    C.ensure_dirs()
    print("hs300_score: load index…")
    idx = load_index()
    print(f"  index bars={len(idx)} {idx['date'].iloc[0].date()}→{idx['date'].iloc[-1].date()}")
    print("hs300_score: load option…")
    opt = load_option()
    print(f"  option rows={len(opt)}")
    print("hs300_score: compute limit-up ratio…")
    zt = compute_limit_up_ratio()
    print(f"  zt days={len(zt)}")
    raw = build_raw_features(idx, opt, zt)
    scored = score_frame(raw)
    payload = latest_payload(scored, opt_days=len(opt))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    v1 = payload["signals"]["v1"]
    v2 = payload["signals"]["v2"]
    print(f"hs300_score: v1={v1['score']} {v1['stance_zh']} | "
          f"v2={v2['score']} {v2['stance_zh']} | "
          f"series={len(payload['series'])} → {OUT}")
    return payload


def main():
    build()


if __name__ == "__main__":
    main()
