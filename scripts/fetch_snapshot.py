#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上午实时快照（"当天上午的结果"）—— 门户 12:00 任务调用：
  1) 32 只 ETF 实时行情（腾讯 qt.gtimg.cn 批量）—— 上午 11:30 收盘后的最新价
  2) 主要指数实时（腾讯 sh000001/sh000300/sh000905/sh000852）
  3) 红利低波 H30269 盘中实时（东财 2.H30269，价格指数信号口径；取不到则标记 null）

输出: data/snapshot/etf_YYYYMMDD.parquet / index_YYYYMMDD.parquet（同日覆盖，幂等）
用法: python3 scripts/fetch_snapshot.py
"""
import datetime
import os
import sys

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

INDEX_SYMS = ["sh000001", "sh000300", "sh000905", "sh000852"]


def tencent_batch(s: requests.Session, syms: list) -> dict:
    """腾讯批量快照 -> {sym: {price, prev_close, open, high, low, volume, amount}}"""
    out = {}
    for i in range(0, len(syms), 60):
        batch = syms[i:i + 60]
        try:
            r = s.get("https://qt.gtimg.cn/q=" + ",".join(batch), timeout=15)
            r.encoding = "gbk"
        except Exception:
            continue
        for line in r.text.split(";"):
            line = line.strip()  # 响应行带 \n 前缀，strip 后才能拿到干净 sym
            if not line or "=" not in line:
                continue
            sym = line.split("=", 1)[0].replace("v_", "")
            parts = line.split("=", 1)[1].strip('"').split("~")
            if len(parts) < 38:
                continue
            try:
                out[sym] = {"price": float(parts[3]), "prev_close": float(parts[4]),
                            "open": float(parts[5]), "high": float(parts[33]),
                            "low": float(parts[34]), "volume": float(parts[6]),
                            "amount": float(parts[37]) * 1e4 if parts[37] else 0.0}
            except ValueError:
                continue
    return out


def em_csi(s: requests.Session, secid: str) -> dict | None:
    """东财指数实时（红利低波 H30269 价格指数信号口径）。"""
    try:
        r = s.get(f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
                  "&fields=f43,f57,f58,f60,f170", timeout=10,
                  headers={"User-Agent": C.UA, "Referer": "https://quote.eastmoney.com/"})
        d = (r.json() or {}).get("data") or {}
        if d.get("f43") is None:
            return None
        return {"price": d["f43"] / 100.0, "prev_close": d.get("f60", d["f43"]) / 100.0}
    except Exception:
        return None


def csi_close_fallback(code: str) -> dict | None:
    """东财取不到 H30269 实时时，回退到日K归档最近两个完整收盘值（12:00 即 T-1）。"""
    from fetch_index import last_complete_day
    p = f"{C.INDEX_DIR}/{code}.parquet"
    d = C.read_df(p)
    if len(d) < 2:
        return None
    cutoff = pd.Timestamp(last_complete_day())
    d = d[d["date"] <= cutoff].sort_values("date")
    if len(d) < 2:
        return None
    last, prev = d.iloc[-1], d.iloc[-2]
    return {"price": float(last["close"]), "prev_close": float(prev["close"])}


def main() -> None:
    C.ensure_dirs()
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": C.UA})
    day = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
           .date().strftime("%Y%m%d"))  # 北京时区（CI 12:00=UTC 04:00 无跨天）
    fetched_at = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
                  .strftime("%Y-%m-%d %H:%M"))

    # ---- ETF 实时 ----
    etf_meta = C.read_df(f"{C.META}/etfs.parquet")
    syms, sym2code = [], {}
    for _, row in etf_meta.iterrows():
        sym = ("sh" if row["code"].startswith(("5", "6")) else "sz") + row["code"]
        syms.append(sym)
        sym2code[sym] = row["code"]
    tx = tencent_batch(s, syms)
    rows = []
    for sym, q in tx.items():
        code = sym2code.get(sym)
        if code is None:
            continue
        m = etf_meta[etf_meta["code"] == code].iloc[0]
        chg = (q["price"] / q["prev_close"] - 1) * 100 if q["prev_close"] else 0.0
        rows.append({"code": code, "name": m["name"], "industry": m["industry"],
                     "price": round(q["price"], 4), "prev_close": round(q["prev_close"], 4),
                     "change_pct": round(chg, 2), "open": q["open"], "high": q["high"],
                     "low": q["low"], "volume": q["volume"], "amount": q["amount"]})
    if rows:
        df = pd.DataFrame(rows)
        df.insert(0, "date", pd.Timestamp(day))
        df.attrs["fetched_at"] = fetched_at
        C.write_df(df, f"{C.SNAP_DIR}/etf_{day}.parquet", sort=["code"])
        print(f"[snapshot] ETF 实时: {len(df)} 只 @ {fetched_at}")

    # ---- 指数实时 ----
    irows = []
    for sym, q in tencent_batch(s, INDEX_SYMS).items():
        chg = (q["price"] / q["prev_close"] - 1) * 100 if q["prev_close"] else 0.0
        irows.append({"code": sym[2:], "name": sym, "price": round(q["price"], 2),
                      "prev_close": round(q["prev_close"], 2), "change_pct": round(chg, 2)})
    hl = em_csi(s, "2.H30269")
    hl_src = "em" if hl else "archive"
    hl = hl or csi_close_fallback("H30269")
    if hl:
        irows.append({"code": "H30269", "name": "红利低波(H30269)",
                      "price": round(hl["price"], 2), "prev_close": round(hl["prev_close"], 2),
                      "change_pct": round((hl["price"] / hl["prev_close"] - 1) * 100, 2),
                      "src": hl_src})
    if irows:
        idf = pd.DataFrame(irows)
        idf.insert(0, "date", pd.Timestamp(day))
        C.write_df(idf, f"{C.SNAP_DIR}/index_{day}.parquet", sort=["code"])
        print(f"[snapshot] 指数实时: {len(idf)} 个 @ {fetched_at}")

    C.manifest_add({"event": "fetch_snapshot", "at": C.bj_now(), "day": day,
                    "etf": len(rows), "index": len(irows)})


if __name__ == "__main__":
    main()
