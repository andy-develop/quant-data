#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 日K 增量更新：
  主通道 东财 push2his(klt=101 fqt=1 前复权，与 sector 归档同口径)；
  兜底通道 腾讯 fqkline(前复权，数值与东财 fqt=1 一致)——东财对 CI IP 连接级限流(RemoteDisconnected)时自动切换。

盘中调用只保留截至 last_complete_day 的完整 bar（12:00 门户任务取 T-1，避免半截 bar）。
用法: python3 scripts/fetch_etf.py
"""
import datetime
import os
import re
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from fetch_index import last_complete_day  # noqa: E402

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _tx_symbol(code: str) -> str:
    """ETF 场内代码 -> 腾讯行情符号（5/6 开头沪，其余深）。"""
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def fetch_em_kline(s: requests.Session, secid: str, start: str, end: str) -> list:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&beg={start}&end={end}")
    last = None
    for k in range(5):
        try:
            r = s.get(url, headers={"User-Agent": C.UA, "Referer": "https://quote.eastmoney.com/"},
                      timeout=30)
            d = (r.json() or {}).get("data") or {}
            if d.get("klines"):
                return d["klines"]
            last = ValueError("empty klines")
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch EM kline {secid} failed: {last}")


def fetch_tx_kline(s: requests.Session, sym: str, start: str, end: str) -> list:
    """腾讯 ETF 日K（fqkline 前复权口径与东财 fqt=1 一致）；主机池故障转移。"""
    out: dict[str, list] = {}
    e = end
    while True:
        bars = C.tx_fqkline_get(s, sym, start, e, chunk=2000)
        if not bars:
            break
        for b in bars:
            out[b[0]] = b
        first = bars[0][0]
        if first <= start:
            break
        e = (pd.Timestamp(first) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.4)
    return [out[k] for k in sorted(out) if k >= start]


def _rows_from_em(klines: list) -> list:
    """东财 klines CSV -> rows（防御：跳过异常行）。"""
    rows = []
    for k in klines:
        parts = k.split(",") if isinstance(k, str) else list(k)
        if len(parts) < 7 or not _DATE_RE.match(str(parts[0])):
            continue
        try:
            rows.append({"date": pd.to_datetime(parts[0]), "open": float(parts[1]),
                         "close": float(parts[2]), "high": float(parts[3]), "low": float(parts[4]),
                         "volume": float(parts[5]),
                         "amount": float(parts[6]) if parts[6] else 0.0,
                         "turnover": float(parts[10]) if len(parts) > 10 and parts[10] else 0.0})
        except (ValueError, TypeError):
            continue
    return rows


def _rows_from_tx(bars: list) -> list:
    """腾讯 6 字段 bars -> rows（无 amount/turnover，置 0）。"""
    rows = []
    for b in bars:
        try:
            rows.append({"date": pd.to_datetime(b[0]), "open": float(b[1]), "close": float(b[2]),
                         "high": float(b[3]), "low": float(b[4]), "volume": float(b[5]),
                         "amount": 0.0, "turnover": 0.0})
        except (ValueError, TypeError, IndexError):
            continue
    return rows


def update() -> int:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": C.UA})
    p = f"{C.ETF_DIR}/etf_kline.parquet"
    old = C.read_df(p)
    meta = C.read_df(f"{C.META}/etfs.parquet")
    if meta.empty:
        print("[fetch_etf] 无 ETF 清单, 跳过")
        return 0
    cutoff = pd.Timestamp(last_complete_day())
    end = datetime.date.today().strftime("%Y%m%d")
    end_dash = datetime.date.today().isoformat()
    last_by = {c: d.max() for c, d in old.groupby("code")["date"]}
    frames = []
    em_ok = tx_ok = skipped = 0
    prefer_tx = os.environ.get("PREFER_TX") == "1"  # CI 上东财封锁 runner IP，直连腾讯
    for _, row in meta.iterrows():
        last = last_by.get(row["code"])
        start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d") if last is not None else "20150101"
        if last is not None and last >= cutoff:
            skipped += 1
            continue
        start_dash = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if last is not None else "2015-01-01"
        source = "tx" if prefer_tx else "em"
        rows = []
        if source == "em":
            try:
                rows = _rows_from_em(fetch_em_kline(s, row["secid"], start, end))
                em_ok += 1
            except Exception as e:
                print(f"[fetch_etf] {row['code']} 东财失败({e}), 切腾讯兜底")
                source = "tx"
        if source == "tx":
            try:
                rows = _rows_from_tx(fetch_tx_kline(s, _tx_symbol(row["code"]), start_dash, end_dash))
                if rows:
                    tx_ok += 1
                elif prefer_tx:
                    # PREFER_TX=1 时腾讯空/失败 → 回退东财（隐藏单点：CI 曾只走腾讯）
                    print(f"[fetch_etf] {row['code']} 腾讯无数据，回退东财")
                    try:
                        rows = _rows_from_em(fetch_em_kline(s, row["secid"], start, end))
                        em_ok += 1
                    except Exception as e2:
                        print(f"[fetch_etf] {row['code']} 东财回退也失败: {e2}")
                        continue
                else:
                    print(f"[fetch_etf] {row['code']} 腾讯无数据")
                    continue
            except Exception as e2:
                if prefer_tx:
                    print(f"[fetch_etf] {row['code']} 腾讯失败({e2}), 回退东财")
                    try:
                        rows = _rows_from_em(fetch_em_kline(s, row["secid"], start, end))
                        em_ok += 1
                    except Exception as e3:
                        print(f"[fetch_etf] {row['code']} 东财回退也失败: {e3}")
                        continue
                else:
                    print(f"[fetch_etf] {row['code']} 腾讯失败: {e2}")
                    continue
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if last is not None:
            df = df[(df["date"] > last) & (df["date"] <= cutoff)]
        else:
            df = df[df["date"] <= cutoff]
        if df.empty:
            continue
        df.insert(0, "code", row["code"])
        df.insert(1, "name", row["name"])
        df.insert(2, "industry", row["industry"])
        frames.append(df)
        time.sleep(0.35)  # 东财限流窗口
    if frames:
        new = pd.concat(frames, ignore_index=True)
        df = pd.concat([old, new], ignore_index=True).drop_duplicates(["code", "date"])
        df = df.sort_values(["code", "date"]).reset_index(drop=True)
        C.write_df(df, p, sort=["code", "date"])
        print(f"[fetch_etf] +{len(new)} 行 -> {len(df):,} 行 (最新 {df['date'].max().date()})"
              f" [东财 {em_ok} / 腾讯 {tx_ok} / 跳过 {skipped}]")
        # 更新清单数据范围
        m = df.groupby("code").agg(start=("date", "min"), end=("date", "max"), n=("date", "count")).reset_index()
        meta = meta.merge(m, on="code", how="left", suffixes=("", "_y"))
        for c_ in ("start", "end", "n"):
            meta[c_] = meta.get(f"{c_}_y", meta[c_]).fillna(meta[c_])
        C.write_df(meta[["code", "name", "industry", "kind", "secid", "start", "end", "n"]],
                   f"{C.META}/etfs.parquet", sort=["code"])
    else:
        print(f"[fetch_etf] 无新增 (截止 {cutoff.date()}) [东财 {em_ok} / 腾讯 {tx_ok} / 跳过 {skipped}]")
    return len(C.read_df(p))


if __name__ == "__main__":
    C.ensure_dirs()
    update()
    C.manifest_add({"event": "fetch_etf", "at": C.bj_now()})
    print("ETF 更新完成")
