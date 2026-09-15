#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘天气 · 天气预计算（Python 复刻 ai-timing-backtest/index.html 的四层策略）。

数据源：quant-data 数据库 C.INDEX_DIR 下锚定指数的日K parquet（OHLCV 全字段）。
锚定指数与页面 IDX_NAME 一致：上证指数 000001 / 深证成指 399001 / 沪深300 000300 /
创业板指 399006 / 中证2000 932000（腾讯 fqkline + 中证官网 index-perf 双通道补齐）。

输出：data/payload/weather.json（供 build_portal.py 注入门户 __WEATHER__ 占位符）：
  {
    "gen_time": "…", "data_date": "2026-09-14",
    "indices": { code: {
        "name": "上证指数", "secid": "sh000001",
        "rows": [[date,o,c,h,l,v], ...],          // 全量 K 线（前端画图 + 回测）
        "ma":  [{"d":date,"st":state,"sub":sub,"p":pos,"pct":pct}, ...],   // 均线状态机
        "vp":  [{"d":date,"p":pos,"tags":[...]}, ...],                     // 量价网格 C
        "fu":  [{"d":date,"p":pos,"mp":maPos,"vp":vpPos,"b":binding,"st":state}, ...],  // 融合 blendC
        "w":   { // 最新一个完整交易日的「大盘天气」
            "date":…, "price":…, "pct":…,
            "maState":…, "maSub":…, "maPos":…, "maNote":…,
            "vpPos":…, "vpTags":[…], "vpNote":…,
            "sqClear":…, "sqNote":…,
            "fuPos":…, "fuBinding":…, "fuNote":…
        }
    } }
  }

复刻口径（与 index.html FIXED 一致）：R0=1.0 / fuseMode=blendC / fuseW=0.5 /
vpLevels=4 / vetoMode=any / squeezeVeto=true / sqVRTHIN=0.8 / sqFilterMA=20。

用法: python3 scripts/build_weather.py
"""
import datetime
import json
import math
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

# ===== 常量（与 index.html 逐字一致） =====
FIXED = dict(R0=1.0, fuseMode="blendC", fuseW=0.5, vpLevels=4,
             vetoMode="any", squeezeVeto=True, sqVRTHIN=0.8, sqFilterMA=20)
VP_LV_V = {0: 0, 1: 0, 2: 0, 3: 0.30, 5: 0.50, 8: 0.50}   # vpLevels=4
VP_LV_N = {0: "清仓", 1: "清仓", 2: "清仓", 3: "三成", 5: "半仓", 8: "半仓"}
BT_POS = {"Z0": 0, "Z1": 0.30, "Z2": 0.50, "Z3": 1.00}
BT_TH = dict(TREND=55, PBREAK=10, ABOVE20=20, DROP=-1.0)
BT_STATE_NAME = {"Z0": "空仓", "Z1": "轻仓", "Z2": "半仓", "Z3": "重仓"}
VP = dict(MA60_HIGH=1.10, MA60_LOW=0.92, VR_STRONG=1.8, VR_UP=1.3, VR_WEAK=0.8,
          VR_DRY=0.6, DROP_BIG=-1.5, FLAT=0.5, SHADOW=0.3, R_HIGH=1.5,
          R_EXTREME=2.5, R_DRY=0.5, R_MID_LO=0.8, R_MID_HI=1.5, V_HUGE=2.0,
          V_DRY=0.6, NEAR60=0.02, RISE=1.5, LB=60)
CN = {0: 0, 1: 0.10, 2: 0.20, 3: 0.30, 5: 0.50, 8: 0.80}
SQ = dict(N=7, FAC=0.6, PT=10, VRTHIN=0.8)

# 锚定指数：代码 → (secid, 名称)。与 index.html IDX_NAME 完全一致。
INDEX_META = {
    "000001": ("sh000001", "上证指数"),
    "399001": ("sz399001", "深证成指"),
    "000300": ("sh000300", "沪深300"),
    "399006": ("sz399006", "创业板指"),
    "932000": ("sh932000", "中证2000"),
}

# ===== 工具函数（与 index.html 一致） =====


def sma(vals, n):
    """简单移动平均；前 n-1 个为 NaN。与 JS sma() 一致。"""
    out = [float("nan")] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def max_arr(a):
    return max(a) if a else float("-inf")


def min_arr(a):
    return min(a) if a else float("inf")


def round2(x):
    return round(x * 100) / 100


# ===== 策略A：均线状态机（index.html computeMaStrategy） =====


def compute_ma_strategy(rows):
    n = len(rows)
    closes = [r["close"] for r in rows]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    state, sub, P = "Z0", "Z0-1", None
    per_day, events = [], []
    for i in range(n):
        r = rows[i]
        c = r["close"]
        m5, m10, m20 = ma5[i], ma10[i], ma20[i]
        if m5 is None or math.isnan(m5) or math.isnan(m10) or math.isnan(m20):
            continue
        pm5 = ma5[i - 1] if i > 0 else float("nan")
        pm10 = ma10[i - 1] if i > 0 else float("nan")
        pct = (c / closes[i - 1] - 1) * 100 if i > 0 else 0.0
        turn_up = (not math.isnan(pm5) and not math.isnan(pm10)
                   and m5 > pm5 and m10 > pm10)
        cond_s0 = m5 < m10 and m5 < m20
        cond_s02 = m5 < m20 and pct < BT_TH["DROP"]
        spread = m20 - m5
        bull = c > m5 and m5 > m10 and m10 > m20

        if state != "Z0" and (cond_s0 or cond_s02):
            act = "S0" if cond_s0 else "S0-2"
            events.append({"date": r["date"], "action": act, "from": state, "to": "Z0",
                           "price": round2(c)})
            state, sub, P = "Z0", ("Z0-2" if spread > BT_TH["TREND"] else "Z0-1"), None
        elif state == "Z0":
            sub = "Z0-2" if spread > BT_TH["TREND"] else "Z0-1"
            if turn_up:
                if sub == "Z0-2":
                    P = r["low"]
                    events.append({"date": r["date"], "action": "B1", "from": "Z0",
                                   "to": "Z1", "price": round2(c)})
                    state, sub = "Z1", "Z1"
                else:
                    events.append({"date": r["date"], "action": "B1-1", "from": "Z0",
                                   "to": "Z1", "price": round2(c)})
                    state, sub = "Z1", "Z1-1"
        elif state == "Z1":
            if P is not None and (P - c) > BT_TH["PBREAK"]:
                events.append({"date": r["date"], "action": "S0-1", "from": "Z1",
                               "to": "Z0", "price": round2(c)})
                state, sub, P = "Z0", ("Z0-2" if spread > BT_TH["TREND"] else "Z0-1"), None
            elif (c - m20) > BT_TH["ABOVE20"]:
                events.append({"date": r["date"], "action": "B2", "from": "Z1",
                               "to": "Z2", "price": round2(c)})
                state, sub = "Z2", "Z2"
            elif bull:
                events.append({"date": r["date"], "action": "B3", "from": "Z1",
                               "to": "Z3", "price": round2(c)})
                state, sub = "Z3", "Z3"
        elif state == "Z2":
            if (m20 - c) > 0:
                events.append({"date": r["date"], "action": "S1", "from": "Z2",
                               "to": "Z1", "price": round2(c)})
                state, sub = "Z1", "Z1-2"
            elif bull:
                events.append({"date": r["date"], "action": "B3", "from": "Z2",
                               "to": "Z3", "price": round2(c)})
                state, sub = "Z3", "Z3"
        elif state == "Z3":
            if (m10 - c) > 0:
                events.append({"date": r["date"], "action": "S2", "from": "Z3",
                               "to": "Z2", "price": round2(c)})
                state, sub = "Z2", "Z2"
            elif (m20 - c) > 0:
                events.append({"date": r["date"], "action": "S1", "from": "Z3",
                               "to": "Z1", "price": round2(c)})
                state, sub = "Z1", "Z1-2"

        day_act = events[-1]["action"] if events and events[-1]["date"] == r["date"] else None
        per_day.append({"date": r["date"], "state": state, "sub": sub,
                        "pos": BT_POS[state], "tags": [day_act] if day_act else [],
                        "pct": pct, "price": c})
    return per_day, events


# ===== 策略B：量价网格重写 C 变体（computeVolPriceCStrategy） =====


def compute_vp_c_strategy(rows, r0=None):
    r0 = FIXED["R0"] if (r0 is None or not (r0 > 0)) else r0
    n = len(rows)
    open_ = [r["open"] for r in rows]
    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]
    vol = [r["volume"] for r in rows]
    ma5 = sma(close, 5)
    ma10 = sma(close, 10)
    ma20 = sma(close, 20)
    ma60 = sma(close, 60)
    vma20 = sma(vol, 20)
    vma30 = sma(vol, 30)
    amt = [c * v for c, v in zip(close, vol)]
    amt_ma60 = sma(amt, 60)
    obv = [0.0] * n
    for i in range(1, n):
        d = close[i] - close[i - 1]
        obv[i] = obv[i - 1] + (vol[i] if d > 0 else (-vol[i] if d < 0 else 0))
    vr = [None] * n
    R = [None] * n
    for i in range(n):
        if vma20[i] and not math.isnan(vma20[i]) and vma20[i] > 0:
            vr[i] = vol[i] / vma20[i]
        if amt_ma60[i] and not math.isnan(amt_ma60[i]) and amt_ma60[i] > 0:
            R[i] = amt[i] / amt_ma60[i] * r0

    def pct_at(i):
        return (close[i] / close[i - 1] - 1) * 100 if (i > 0 and close[i - 1] > 0) else 0.0

    def shadow_at(i):
        return (high[i] - max(open_[i], close[i])) / close[i] * 100 if close[i] > 0 else 0.0

    def hi60_at(i):
        return max_arr(close[i - VP["LB"] + 1: i + 1]) if i >= VP["LB"] - 1 else None

    def lo60_at(i):
        return min_arr(close[i - VP["LB"] + 1: i + 1]) if i >= VP["LB"] - 1 else None

    def prev_high_vol_at(i):
        return max_arr(vol[i - VP["LB"]: i]) if i >= VP["LB"] else None

    def stall_at(i):
        if i < 0 or ma60[i] is None or math.isnan(ma60[i]) or vr[i] is None or R[i] is None:
            return False
        return (close[i] / ma60[i]) > VP["MA60_HIGH"] and vr[i] > VP["VR_STRONG"] and \
            (abs(pct_at(i)) < VP["FLAT"] or shadow_at(i) > VP["SHADOW"]) and R[i] > VP["R_HIGH"]

    def dry_at(i):
        return (vma30[i] is not None and not math.isnan(vma30[i]) and R[i] is not None
                and vol[i] < VP["V_DRY"] * vma30[i]
                and (lo60_at(i) is not None and close[i] <= lo60_at(i) * (1 + VP["NEAR60"]))
                and R[i] < VP["R_DRY"])

    def huge_top_at(j):
        if j < VP["LB"] - 1 or vma30[j] is None or math.isnan(vma30[j]) or R[j] is None:
            return False
        return (vol[j] > VP["V_HUGE"] * vma30[j]
                and close[j] >= hi60_at(j) - 1e-9 and R[j] > VP["R_EXTREME"])

    per_day, events = [], []
    counters = {"rule": 0, "fallback": 0, "carry": 0}
    pos, prev_pos = 0.5, None
    for i in range(n):
        c, v, pct = close[i], vol[i], pct_at(i)
        ready = (not math.isnan(ma60[i]) if ma60[i] is not None else False) and \
            (not math.isnan(vma30[i]) if vma30[i] is not None else False) and \
            vr[i] is not None and R[i] is not None and \
            (not math.isnan(amt_ma60[i]) if amt_ma60[i] is not None else False)
        cand = []
        if ready:
            ratio60 = c / ma60[i]
            hi60 = hi60_at(i)
            lo60 = lo60_at(i)
            new_high = hi60 is not None and c >= hi60 - 1e-9
            near_low = lo60 is not None and c <= lo60 * (1 + VP["NEAR60"])
            sh = shadow_at(i)
            above20 = ma20[i] is not None and not math.isnan(ma20[i]) and c > ma20[i]
            bull = (ma20[i] is not None and not math.isnan(ma20[i])
                    and ma60[i] is not None and not math.isnan(ma60[i])
                    and ma20[i] > ma60[i])
            below_m5 = ma5[i] is not None and not math.isnan(ma5[i]) and c < ma5[i]
            below_m5p = (ma5[i - 1] is not None and not math.isnan(ma5[i - 1])
                         and close[i - 1] < ma5[i - 1]) if i > 0 else False
            div = i > 0 and pct > 0 and v < vol[i - 1]
            divp = (i > 1 and pct_at(i - 1) > 0 and vol[i - 1] < vol[i - 2])
            pv = prev_high_vol_at(i)
            obv_no_high = i >= VP["LB"] and obv[i] <= max_arr(obv[i - VP["LB"]: i]) + 1e-9

            # 防守端
            if ratio60 > VP["MA60_HIGH"] and vr[i] > VP["VR_STRONG"] and \
                    pct < VP["DROP_BIG"] and below_m5:
                cand.append({"pos": 0, "tag": "C0·高位量增价跌", "note": "高位放量下跌，清仓"})
            if stall_at(i) and stall_at(i - 1):
                cand.append({"pos": 0, "tag": "C0·高位放量滞涨2日", "note": "高位放量滞涨连续2日，清仓"})
            if i > 0 and huge_top_at(i - 1) and (open_[i] < close[i - 1] or c < open_[i]):
                cand.append({"pos": 0, "tag": "C0·天量天价次日转弱", "note": "天量天价后次日低开/收阴，清仓"})
            if dry_at(i):
                cand.append({"pos": 0, "tag": "C0·地量地价", "note": "地量地价，空仓观望"})
            if ratio60 < VP["MA60_LOW"] and vr[i] < VP["VR_DRY"] and R[i] < VP["R_DRY"]:
                cand.append({"pos": 0, "tag": "C0·低位无量", "note": "低位无量，空仓等待"})
            if (div and divp) or (below_m5 and ratio60 > VP["MA60_HIGH"]) or \
                    (below_m5 and below_m5p):
                cand.append({"pos": 0, "tag": "C0·量价背离/破位", "note": "量价背离确认或跌破MA5，清仓"})
            if pct < -0.3 and not above20 and not bull:
                cand.append({"pos": 0, "tag": "C0·空头下跌", "note": "空头排列中下跌，空仓"})
            # 1 成
            if ratio60 < VP["MA60_LOW"] and vr[i] > VP["VR_UP"] and pct < VP["DROP_BIG"]:
                cand.append({"pos": VP_LV_V[1], "tag": "C1·低位量增价跌", "note": "低位放量下跌首日 → " + VP_LV_N[1]})
            if vr[i] < VP["VR_WEAK"] and pct < 0 and ma20[i] is not None and \
                    not math.isnan(ma20[i]) and c < ma20[i]:
                cand.append({"pos": VP_LV_V[1], "tag": "C1·量缩价跌", "note": "量缩跌破20日线 → " + VP_LV_N[1]})
            if stall_at(i):
                cand.append({"pos": VP_LV_V[1], "tag": "C1·高位滞涨首日", "note": "高位放量滞涨首日 → " + VP_LV_N[1]})
            if i > 0 and dry_at(i - 1) and pct > 0 and v > vma20[i]:
                cand.append({"pos": VP_LV_V[1], "tag": "C1·地量后放量阳", "note": "地量地价后放量阳线 → " + VP_LV_N[1]})
            if pct < -0.3 and not above20:
                cand.append({"pos": VP_LV_V[1], "tag": "C1·跌破20线", "note": "跌破20日线 → " + VP_LV_N[1]})
            # 进攻端（C 的网格：位置 × 方向 × 量能）——tag 与 index.html 逐字一致
            if ratio60 > VP["MA60_HIGH"] and pct > 0.3:
                cand.append({"pos": VP_LV_V[3] if vr[i] > VP["VR_UP"] else VP_LV_V[5],
                             "tag": "C·高位价升",
                             "note": "高位价升" + ("且放量 → " + VP_LV_N[3] if vr[i] > VP["VR_UP"] else " → " + VP_LV_N[5])})
            elif pct > 0.3:
                if above20 and bull:
                    cand.append({"pos": 1.0 if vr[i] > VP["VR_UP"] else VP_LV_V[8],
                                 "tag": "C·趋势价升",
                                 "note": "趋势价升（收盘>MA20 且 MA20>MA60）" +
                                 ("且放量 → 满仓" if vr[i] > VP["VR_UP"] else " → " + VP_LV_N[8])})
                else:
                    cand.append({"pos": VP_LV_V[5] if vr[i] > VP["VR_UP"] else VP_LV_V[3],
                                 "tag": "C·弱趋势价升",
                                 "note": "弱趋势价升" + ("且放量 → " + VP_LV_N[5] if vr[i] > VP["VR_UP"] else " → " + VP_LV_N[3])})
            elif abs(pct) <= 0.3:
                if ratio60 > VP["MA60_HIGH"]:
                    cand.append({"pos": VP_LV_V[3], "tag": "C·高位价平", "note": "高位滞涨 → " + VP_LV_N[3]})
            else:
                cand.append({"pos": VP_LV_V[3] if above20 else VP_LV_V[1], "tag": "C·价跌",
                             "note": "价跌" + ("但仍在20日线上 → " + VP_LV_N[3]
                                              if above20 else "且跌破20日线 → " + VP_LV_N[1])})

        new_pos = pos
        current = cand[0] if cand else None
        if current:
            new_pos = current["pos"]
            if ready:
                counters["rule"] += 1
        elif ready and pct > 0.3:
            new_pos = VP_LV_V[5]
            current = {"pos": VP_LV_V[5], "tag": "兜底·价涨", "note": "网格未命中，按价格方向兜底至" + VP_LV_N[5]}
            counters["fallback"] += 1
        elif ready and pct < -0.3:
            new_pos = VP_LV_V[1]
            current = {"pos": VP_LV_V[1], "tag": "兜底·价跌", "note": "网格未命中，按价格方向兜底至" + VP_LV_N[1]}
            counters["fallback"] += 1
        elif ready:
            counters["carry"] += 1
        if prev_pos is None:
            prev_pos = new_pos

        per_day.append({"date": rows[i]["date"], "pos": new_pos,
                        "tags": [x["tag"] for x in cand], "pct": pct})
        if abs(new_pos - prev_pos) > 1e-9:
            up = new_pos > prev_pos
            events.append({"date": rows[i]["date"],
                           "action": current["tag"] if current else ("加仓" if up else "减仓"),
                           "from": round2(prev_pos * 100), "to": round2(new_pos * 100),
                           "price": round2(c), "dir": "buy" if up else "sell"})
        pos, prev_pos = new_pos, new_pos
    return per_day, events, counters


# ===== 策略四：均线粘合清仓（computeSqueezeStrategy） =====


def compute_squeeze_strategy(rows):
    vrthin = FIXED["sqVRTHIN"] if FIXED.get("sqVRTHIN") is not None else SQ["VRTHIN"]
    fma = FIXED["sqFilterMA"] if FIXED.get("sqFilterMA") is not None else 0
    n = len(rows)
    close = [r["close"] for r in rows]
    vol = [r["volume"] for r in rows]
    ma5 = sma(close, 5)
    ma10 = sma(close, 10)
    vma20 = sma(vol, 20)
    ma_f = sma(close, fma) if fma > 0 else None
    gap_pct = [None] * n
    gap_pt = [None] * n
    for i in range(n):
        if ma5[i] is not None and not math.isnan(ma5[i]) and \
                ma10[i] is not None and not math.isnan(ma10[i]) and ma10[i] > 0:
            gap_pt[i] = abs(ma5[i] - ma10[i])
            gap_pct[i] = gap_pt[i] / ma10[i] * 100
    per_day, events = [], []
    counters = {"rule": 0, "carry": 0, "squeeze": 0}
    in_sq, x0, prev = False, None, 1
    for i in range(n):
        squeeze = False
        if gap_pct[i] is not None and i >= SQ["N"] + 10:
            if not in_sq:
                x = float("inf")
                for k in range(i - SQ["N"], i):
                    if gap_pct[k] is not None and gap_pct[k] < x:
                        x = gap_pct[k]
                if math.isfinite(x) and gap_pct[i] < SQ["FAC"] * x and gap_pt[i] < SQ["PT"]:
                    in_sq, x0 = True, x
            elif not (gap_pct[i] < SQ["FAC"] * x0 and gap_pt[i] < SQ["PT"]):
                in_sq, x0 = False, None
            squeeze = in_sq
        if squeeze:
            counters["squeeze"] += 1
        thin = (vma20[i] is not None and not math.isnan(vma20[i]) and vma20[i] > 0
                and vol[i] < vrthin * vma20[i])
        below_f = (ma_f is None or (ma_f[i] is not None and not math.isnan(ma_f[i])
                                    and close[i] < ma_f[i]))
        clear = squeeze and thin and below_f
        pos = 0 if clear else 1
        if clear:
            counters["rule"] += 1
        else:
            counters["carry"] += 1
        per_day.append({"date": rows[i]["date"], "pos": pos, "squeeze": squeeze,
                        "thin": thin, "clear": clear, "gapPct": gap_pct[i],
                        "gapPt": gap_pt[i]})
        if abs(pos - prev) > 1e-9:
            up = pos > prev
            events.append({"date": rows[i]["date"],
                           "action": "S4 · 解除清仓" if up else "S4 · 粘合缩量清仓",
                           "from": round2(prev * 100), "to": round2(pos * 100),
                           "price": round2(close[i]), "dir": "buy" if up else "sell"})
        prev = pos
    return per_day, events, counters


# ===== 融合策略 blendC（computeFusionStrategy） =====


def compute_fusion_strategy(ma_per_day, vp_per_day, mode="blendC", w=None, sq_per_day=None):
    if mode not in ("min", "maOverride", "maOverrideRaw", "brake", "blendC"):
        mode = "blendC"
    w = FIXED["fuseW"] if (w is None or not math.isfinite(w)) else max(0.0, min(1.0, w))
    map_vp = {d["date"]: d["pos"] for d in vp_per_day}
    map_sq = {d["date"]: d["pos"] for d in sq_per_day} if sq_per_day else {}
    per_day, events = [], []
    z = 1e-9
    prev = None
    for d in ma_per_day:
        vp_pos = map_vp.get(d["date"], 0.5)
        sq_pos = map_sq.get(d["date"], 1)
        sq_on = sq_per_day is not None and FIXED["squeezeVeto"] is not False
        vp_zero = vp_pos <= z
        ma_zero = d["pos"] <= z
        sq_zero = sq_on and sq_pos <= z
        ma_heavy = d["pos"] >= 1 - z
        if mode == "blendC":
            veto = (vp_zero or sq_zero) if FIXED["vetoMode"] == "c" else (ma_zero or vp_zero or sq_zero)
            if veto:
                pos, binding, rule = 0, "否决", (
                    "均线粘合且缩量 → 清仓" if sq_zero else
                    ("两条策略都空仓 → 清仓" if (ma_zero and vp_zero) else
                     ("均线策略空仓 → 清仓" if ma_zero else "量价策略空仓 → 清仓")))
            elif abs(w - 1) < z:
                pos, binding = d["pos"], "均线"
                rule = "纯跟随均线 " + str(round(d["pos"] * 100)) + "%"
            elif w < z:
                pos, binding = vp_pos, "量价"
                rule = "纯跟随量价 " + str(round(vp_pos * 100)) + "%"
            else:
                pos = w * d["pos"] + (1 - w) * vp_pos
                binding = "混合"
                rule = ("均线 " + str(round(d["pos"] * 100)) + "% × " + format(w, ".2f")
                        + "　+　量价 " + str(round(vp_pos * 100)) + "% × " + format(1 - w, ".2f"))
        else:
            # 其余口径保留代码备查（门户固定 blendC，不渲染）
            if mode == "brake":
                if vp_zero:
                    pos, binding, rule = 0, "否决", "量价策略空仓 → 清仓"
                else:
                    pos = w * d["pos"] + (1 - w) * vp_pos
                    binding = "均线" if (1 - w) < z else ("量价" if w < z else "混合")
                    rule = "均线×" + format(w, ".2f") + " + 量价×" + format(1 - w, ".2f")
            else:
                if mode in ("maOverride", "maOverrideRaw") and (mode == "maOverrideRaw" or not vp_zero):
                    if ma_heavy:
                        pos, binding, rule = 1, "均线", "均线重仓 → 抬至 100%"
                    else:
                        pos = min(d["pos"], vp_pos)
                        binding = "均线" if d["pos"] <= vp_pos else "量价"
                        rule = "取两者较低"
                else:
                    pos = min(d["pos"], vp_pos)
                    binding = "均线" if d["pos"] <= vp_pos else "量价"
                    rule = "取两者较低"
        per_day.append({"date": d["date"], "pos": pos, "maPos": d["pos"],
                        "vpPos": vp_pos, "binding": binding, "state": d["state"],
                        "sqZero": sq_zero, "tags": [binding + "主导"], "note": rule})
        if prev is not None and abs(pos - prev) > 1e-9:
            act = "加仓" if pos > prev else ("策略四清仓" if sq_zero else "减仓")
            events.append({"date": d["date"], "action": act,
                           "from": round2(prev * 100), "to": round2(pos * 100),
                           "price": round2(d["price"]), "dir": "buy" if pos > prev else "sell",
                           "note": rule})
        prev = pos
    return per_day, events, mode


# ===== 主流程 =====


def weather_note(ma_state, ma_sub, ma_pos, vp_pos, fu_pos, sq_clear, pct):
    """生成最新交易日的「天气」描述（前端展示用，口径自洽）。"""
    parts = []
    if ma_state:
        parts.append("均线" + BT_STATE_NAME.get(ma_state, ma_state) +
                     (("·" + ma_sub) if ma_sub and ma_sub != ma_state else ""))
    parts.append("量价" + ("清仓" if vp_pos <= 1e-9 else
                          ("满仓" if vp_pos >= 1 - 1e-9 else "仓位" + str(round(vp_pos * 100)) + "%")))
    if sq_clear:
        parts.append("粘合清仓触发")
    note = " / ".join(parts)
    note += "｜当日" + ("涨" if pct > 0 else "跌") + ("%.2f%%" % abs(pct))
    return note


def build_index(code, meta):
    secid, name = meta
    df = C.read_df(f"{C.INDEX_DIR}/{code}.parquet")
    if not len(df):
        print(f"[build_weather] 警告: 指数 {code} 无数据，跳过")
        return None
    df = df.sort_values("date").reset_index(drop=True)
    rows = [{"date": r.date.strftime("%Y-%m-%d"),
             "open": float(r.open), "close": float(r.close),
             "high": float(r.high), "low": float(r.low), "volume": float(r.volume)}
            for r in df.itertuples()]

    ma_per_day, _ = compute_ma_strategy(rows)
    vp_per_day, _, vp_counters = compute_vp_c_strategy(rows)
    sq_per_day, _, sq_counters = compute_squeeze_strategy(rows)
    fu_per_day, _, _ = compute_fusion_strategy(ma_per_day, vp_per_day, "blendC",
                                                FIXED["fuseW"], sq_per_day)

    # 最新一个完整交易日（rows 最后一行；ma perDay 最后一条日期与之相同）
    last = rows[-1]
    last_ma = ma_per_day[-1]
    last_vp = vp_per_day[-1]
    last_fu = fu_per_day[-1]
    last_sq = next((d for d in sq_per_day if d["date"] == last["date"]), None)
    sq_clear = bool(last_sq and last_sq["clear"])
    weather = {
        "date": last["date"], "price": last["close"], "pct": last_ma["pct"],
        "maState": last_ma["state"], "maSub": last_ma["sub"], "maPos": last_ma["pos"],
        "maNote": "均线状态机 " + last_ma["state"] + "（" +
                  BT_STATE_NAME.get(last_ma["state"], "") + "）",
        "vpPos": last_vp["pos"], "vpTags": last_vp["tags"],
        "vpNote": "量价网格 C" + ("触发: " + " / ".join(last_vp["tags"])
                                  if last_vp["tags"] else "无规则命中，沿用前值"),
        "sqClear": sq_clear,
        "sqNote": "均线粘合且缩量清仓（策略四）" if sq_clear else "策略四未触发清仓",
        "fuPos": last_fu["pos"], "fuBinding": last_fu["binding"],
        "fuNote": weather_note(last_ma["state"], last_ma["sub"], last_ma["pos"],
                               last_vp["pos"], last_fu["pos"], sq_clear, last_ma["pct"]),
    }
    print(f"[build_weather] {code} {name}: {len(rows)} 行 | "
          f"均线{last_ma['state']}({last_ma['pos']:.2f}) 量价{last_vp['pos']:.2f} "
          f"融合{last_fu['pos']:.2f} | 覆盖 规则{vp_counters['rule']}/"
          f"兜底{vp_counters['fallback']}/沿用{vp_counters['carry']} | "
          f"粘合日{sq_counters['squeeze']} 清仓日{sq_counters['rule']}")
    return {
        "name": name, "secid": secid,
        "rows": [[r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"]]
                 for r in rows],
        # perDay 序列由前端本地重算（weather_tab.html 内嵌 index.html 同款四层策略 JS，
        # 本脚本输出与前端 JS 已做交叉验证一致）；这里只注入 K 线与当前天气。
        "w": weather,
    }


def main():
    C.ensure_dirs()
    out = {"gen_time": C.bj_now(), "data_date": "", "indices": {}}
    for code, meta in INDEX_META.items():
        idx = build_index(code, meta)
        if idx:
            out["indices"][code] = idx
    if not out["indices"]:
        print("错误: 无任何指数数据可输出")
        sys.exit(1)
    # data_date = 各指数最新交易日的并集（全部一致时即全局数据日期）
    dates = sorted({v["w"]["date"] for v in out["indices"].values()})
    out["data_date"] = dates[-1] if len(dates) == 1 else dates
    p = f"{C.PAYLOAD_DIR}/weather.json"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(p) / 1024
    print(f"生成完成: {p} ({size_kb:.1f} KB, {len(out['indices'])} 个指数, "
          f"data_date={out['data_date']})")
    C.manifest_add({"event": "build_weather", "at": C.bj_now(),
                    "indices": sorted(out["indices"]), "size_kb": round(size_kb, 1)})


if __name__ == "__main__":
    main()
