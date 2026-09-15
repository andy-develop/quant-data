#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数日K 增量更新：
  1) 中证指数官网（csindex index-perf）：H20269/H30269/H00300/000300 —— 当日值收盘后发布，
     盘中拉取只能拿到 T-1（12:00 门户任务用 T-1 值，符合"策略信号基于完整收盘"口径）。
  2) 腾讯 fqkline：000001/000905/000852（主要指数 10 年）—— 盘中含半截 bar，
     按 last_complete_day 截止清洗（12:00 只保留 T-1 前完整 bar；东财对 CI IP 连接级限流，弃用）。

用法: python3 scripts/fetch_index.py [--backfill]   # --backfill 仅拉全量历史(迁移用)
"""
import datetime
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

UA = {"User-Agent": C.UA, "Referer": "https://www.csindex.com.cn/"}

CSI_CODES = ["H20269", "H30269", "H00300", "000300"]
TX_CODES = {  # 主要指数(10年) — 腾讯 fqkline 主通道（东财对 CI IP 连接级限流，见 red-dividend 台账）
    "000001": ("sh000001", "上证指数"),
    "000905": ("sh000905", "中证500"),
    "000852": ("sh000852", "中证1000"),
}
TX_BACKFILL_START = "2016-01-01"  # 10 年
TX_CHUNK = 2000                   # 腾讯单次最大 bar 数（实测 2000 可一次返回）


def last_complete_day() -> str:
    """当前可用的"最后一个完整交易日"（YYYY-MM-DD）：
    收盘后(>=15:30 北京)取今天，否则取上一交易日；假日自动回退（用交易日历）。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    d = now.date()
    cal = C.read_df(f"{C.META}/trade_calendar.parquet")
    if len(cal):
        days = set(pd.to_datetime(cal["date"]).dt.date)
    else:
        days = None
    if now.time() >= datetime.time(15, 30) and (days is None or d in days):
        return d.isoformat()
    for _ in range(14):
        d -= datetime.timedelta(days=1)
        if days is None or d in days:
            return d.isoformat()
    return d.isoformat()


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(UA)
    return s


def fetch_csi(s: requests.Session, code: str, start: str, end: str) -> list:
    """csindex index-perf 增量区间；空区间(节假日/未发布)也合法返回。"""
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(5):
        try:
            r = s.get(url, timeout=30)
            j = r.json()
            rows = j.get("data") or []
            if rows or start != "20130719":
                return rows
            last = ValueError("empty rows")
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch CSI {code} failed: {last}")


def fetch_tx_kline(s: requests.Session, sym: str, start: str, end: str) -> list:
    """腾讯指数日K：单次最多 2000 根（返回区间末尾 N 根），从 end 向前分页直到覆盖 start。
    返回 bars: [date,open,close,high,low,volume]，按日期升序。"""
    out: dict[str, list] = {}
    e = end
    while True:
        bars: list = []
        last = None
        for k in range(5):
            try:
                r = s.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                          params={"param": f"{sym},day,{start},{e},{TX_CHUNK},"}, timeout=15)
                d = (r.json() or {}).get("data", {}).get(sym) or {}
                bars = [x for x in (d.get("day") or []) if isinstance(x, list) and len(x) >= 6]
                if bars:
                    break
                last = ValueError("empty bars")
            except Exception as ex:
                last = ex
            time.sleep(1.5 * (k + 1))
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


def _csi_to_df(code: str, rows: list) -> pd.DataFrame:
    df = pd.DataFrame([{"date": pd.to_datetime(r["tradeDate"]), "close": float(r["close"])}
                       for r in rows])
    df.insert(0, "code", code)
    return df


def _tx_to_df(code: str, bars: list) -> pd.DataFrame:
    """腾讯 fqkline bars: [date,open,close,high,low,volume]（无 amount，置 0）"""
    rows = []
    for k in bars:
        try:
            rows.append({"date": pd.to_datetime(k[0]), "open": float(k[1]),
                         "close": float(k[2]), "high": float(k[3]), "low": float(k[4]),
                         "volume": float(k[5]), "amount": 0.0})
        except (ValueError, TypeError, IndexError):
            continue
    df = pd.DataFrame(rows)
    df.insert(0, "code", code)
    return df


def update_csi(s: requests.Session, code: str) -> int:
    p = f"{C.INDEX_DIR}/{code}.parquet"
    old = C.read_df(p)
    last = old["date"].max() if len(old) else pd.Timestamp("2013-07-19")
    today = datetime.date.today().strftime("%Y%m%d")
    start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
    if start > today:
        return len(old)
    rows = fetch_csi(s, code, start, today)
    if not rows:
        return len(old)
    new = _csi_to_df(code, rows)
    new = new[new["date"] > last]
    if new.empty:
        return len(old)
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    C.write_df(df, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: +{len(new)} 行 -> {len(df)} 行 ({df['date'].max().date()})")
    return len(df)


def update_tx(s: requests.Session, code: str, sym: str) -> int:
    """腾讯通道增量更新主要指数（10 年口径，回填用 --backfill）。"""
    p = f"{C.INDEX_DIR}/{code}.parquet"
    old = C.read_df(p)
    last = old["date"].max() if len(old) else pd.Timestamp(TX_BACKFILL_START)
    # 完整 bar 截止日（避免盘中半截）
    cutoff = pd.Timestamp(last_complete_day())
    end = datetime.date.today().isoformat()
    start = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if start > end:
        return len(old)
    bars = fetch_tx_kline(s, sym, start, end)
    new = _tx_to_df(code, bars)
    if new.empty:  # 无新增（已是最新/节假日）：合法空增量，不构造过滤
        return len(old)
    new = new[(new["date"] > last) & (new["date"] <= cutoff)]
    if new.empty:
        return len(old)
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    C.write_df(df, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: +{len(new)} 行 -> {len(df)} 行 ({df['date'].max().date()})")
    return len(df)


def main() -> None:
    C.ensure_dirs()
    backfill = "--backfill" in sys.argv
    s = make_session()
    for code in CSI_CODES:
        update_csi(s, code)
    for code, (sym, name) in TX_CODES.items():
        if backfill and os.path.exists(f"{C.INDEX_DIR}/{code}.parquet"):
            # 回填：删除 3 年短序列，全量重拉 10 年
            os.remove(f"{C.INDEX_DIR}/{code}.parquet")
        update_tx(s, code, sym)
    # 更新指数清单
    idx = C.read_df(f"{C.META}/indices.parquet")
    if len(idx):
        for _, row in idx.iterrows():
            p = f"{C.INDEX_DIR}/{row['code']}.parquet"
            if os.path.exists(p):
                d = pd.read_parquet(p)
                if len(d):
                    idx.loc[_, "start"] = str(d["date"].min().date())
                    idx.loc[_, "end"] = str(d["date"].max().date())
                    idx.loc[_, "n"] = len(d)
        C.write_df(idx, f"{C.META}/indices.parquet", sort=["code"])
    C.manifest_add({"event": "fetch_index", "at": C.bj_now(), "backfill": backfill})
    print("指数更新完成")


if __name__ == "__main__":
    main()
