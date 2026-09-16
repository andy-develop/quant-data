#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘天气 · 择时回测取数包：从 quant-data parquet 组装 TIMING_DB。

输出 data/payload/timing_db.json，供 build_portal.py 注入 bt_tab.html 的
/*__TIMING_DB__*/{} 。结构：
  {
    "gen_time": "...", "data_date": "YYYY-MM-DD",
    "indices": { "000001": {"name": "...", "secid": "sh000001",
                            "rows": [[date,o,c,h,l,v], ...] }, ... },
    "stocks":  { "600519": {"name": "...", "hfq": [[date,o,c,h,l,0], ...] } }
  }

取数规则：
  1) 指数：读 kline/index/{code}.parquet；缺失或过旧则调用 fetch_index 补齐后再读。
  2) 股票：读 kline/stock/hfq_*.parquet（后复权，回测口径）；默认缓存常用标的
     （600519 等）；缺失则调用 fetch_stock 单股补齐（腾讯 fqkline）后再读。

用法: python3 scripts/build_timing_db.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

# 与 build_weather 一致：门户注入截断，parquet 仍保留全历史
PAYLOAD_KEEP_YEARS = int(os.environ.get("PAYLOAD_KEEP_YEARS", "5"))

# 与 index.html 锚定指数一致
INDEX_META = {
    "000001": ("sh000001", "上证指数"),
    "399001": ("sz399001", "深证成指"),
    "000300": ("sh000300", "沪深300"),
    "399006": ("sz399006", "创业板指"),
    "932000": ("sh932000", "中证2000"),
}

# 回测页默认/常用交易标的（后复权）；可按需扩展
DEFAULT_STOCKS = [
    ("1.600519", "600519", "贵州茅台"),
    ("0.000001", "000001", "平安银行"),
    ("0.300750", "300750", "宁德时代"),
]


def _bj_today() -> str:
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d")


def _rows_from_index_df(df: pd.DataFrame) -> list:
    """[[date, open, close, high, low, volume], ...] 升序。"""
    if df is None or df.empty:
        return []
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    vol_col = "volume" if "volume" in d.columns else None
    out = []
    for r in d.sort_values("date").itertuples(index=False):
        vol = float(getattr(r, vol_col)) if vol_col else 0.0
        out.append([
            r.date,
            round(float(r.open), 4),
            round(float(r.close), 4),
            round(float(r.high), 4),
            round(float(r.low), 4),
            vol,
        ])
    return out


def _rows_from_stock_df(df: pd.DataFrame) -> list:
    """hfq 无 volume → vol 置 0（策略信号走锚定指数，标的只需 OHLC）。"""
    if df is None or df.empty:
        return []
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    out = []
    for r in d.sort_values("date").itertuples(index=False):
        out.append([
            r.date,
            round(float(r.open), 4),
            round(float(r.close), 4),
            round(float(r.high), 4),
            round(float(r.low), 4),
            0.0,
        ])
    return out


def _ensure_indices() -> None:
    """缺任一锚定指数 parquet 时跑 fetch_index 补齐。"""
    missing = [
        c for c in INDEX_META
        if not os.path.exists(f"{C.INDEX_DIR}/{c}.parquet")
    ]
    if not missing:
        # 检查是否空表
        for c in INDEX_META:
            df = C.read_df(f"{C.INDEX_DIR}/{c}.parquet")
            if df.empty:
                missing.append(c)
    if not missing:
        return
    print(f"[build_timing_db] 指数缺失 {missing}，调用 fetch_index 补取…")
    import fetch_index as FI  # noqa: WPS433
    FI.main()


def _sym_from_tx(tx: str) -> str:
    """1.600519 → sh600519；0.000001 → sz000001。"""
    mkt, code = tx.split(".")
    return ("sh" if mkt == "1" else "sz") + code


def _ensure_stock(tx: str, code6: str) -> pd.DataFrame:
    """从 hfq 分片取一只；不足则调 fetch_stock.fqkline 补一段后写入 incr。"""
    import duckdb

    con = duckdb.connect()
    pattern = f"{C.STOCK_DIR}/hfq_*.parquet"
    try:
        df = con.execute(
            f"SELECT * FROM read_parquet('{pattern}') WHERE code = ? ORDER BY date",
            [tx],
        ).df()
    except Exception:
        df = pd.DataFrame()
    if len(df) >= 60:
        return df

    print(f"[build_timing_db] 股票 {code6} 本地不足（{len(df)} 行），补取 hfq…")
    try:
        import fetch_stock as FS  # noqa: WPS433
        end = _bj_today()
        start = (datetime.date.today() - datetime.timedelta(days=365 * 4)).isoformat()
        sym = _sym_from_tx(tx)
        new = FS.fetch_fqkline(sym, start, end, hfq=True)
        if new is None or new.empty:
            print(f"[build_timing_db] 警告: {code6} 补取失败，沿用已有 {len(df)} 行")
            return df
        new = new.copy()
        if "code" not in new.columns:
            new["code"] = tx
        day = end.replace("-", "")
        incr = f"{C.STOCK_DIR}/hfq_incr_{day}.parquet"
        old_incr = C.read_df(incr)
        merged = pd.concat([old_incr, new], ignore_index=True) if len(old_incr) else new
        C.write_df(merged, incr, sort=["code", "date"])
        print(f"[build_timing_db] {code6} 补取 {len(new)} 行 → {incr}")
        return (pd.concat([df, new], ignore_index=True)
                  .drop_duplicates(["code", "date"], keep="last")
                  .sort_values("date"))
    except Exception as e:
        print(f"[build_timing_db] 警告: {code6} 补取异常: {e}")
        return df

def _trim_rows(rows: list) -> list:
    """按末日期回看 PAYLOAD_KEEP_YEARS 截断 [[date,...], ...]。"""
    if not rows or PAYLOAD_KEEP_YEARS <= 0:
        return rows
    last = pd.Timestamp(rows[-1][0])
    cutoff = (last - pd.DateOffset(years=PAYLOAD_KEEP_YEARS)).strftime("%Y-%m-%d")
    return [r for r in rows if r[0] >= cutoff]


def build() -> dict:
    C.ensure_dirs()
    _ensure_indices()

    indices = {}
    index_dates = []
    for code, (secid, name) in INDEX_META.items():
        df = C.read_df(f"{C.INDEX_DIR}/{code}.parquet")
        rows = _trim_rows(_rows_from_index_df(df))
        if not rows:
            print(f"[build_timing_db] 警告: 指数 {code} 仍无数据")
            continue
        indices[code] = {"name": name, "secid": secid, "rows": rows}
        index_dates.append(rows[-1][0])
        print(f"[build_timing_db] 指数 {code} {name}: {len(rows)} 行 "
              f"({rows[0][0]} → {rows[-1][0]})")

    stocks = {}
    stock_dates = []
    for tx, code6, name in DEFAULT_STOCKS:
        df = _ensure_stock(tx, code6)
        rows = _trim_rows(_rows_from_stock_df(df))
        if not rows:
            print(f"[build_timing_db] 警告: 股票 {code6} 无数据，跳过")
            continue
        stocks[code6] = {"name": name, "tx": tx, "hfq": rows}
        stock_dates.append(rows[-1][0])
        print(f"[build_timing_db] 股票 {code6} {name}: {len(rows)} 行 "
              f"({rows[0][0]} → {rows[-1][0]})")

    # 以指数 min 为准（股票可略旧）；混日时不虚高
    if not index_dates:
        raise SystemExit("[build_timing_db] 无任何指数数据")
    uniq = sorted(set(index_dates))
    if len(uniq) > 1:
        print(f"[build_timing_db] 警告: 指数混日 {uniq}，data_date 取 min={uniq[0]}")
    data_date = uniq[0]
    payload = {
        "gen_time": C.bj_now() if hasattr(C, "bj_now") else _bj_today(),
        "data_date": data_date,
        "data_dates": uniq,
        "indices": indices,
        "stocks": stocks,
    }
    out = f"{C.PAYLOAD_DIR}/timing_db.json"
    os.makedirs(C.PAYLOAD_DIR, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    size_mb = os.path.getsize(out) / 1e6
    print(f"[build_timing_db] → {out} ({size_mb:.2f} MB) "
          f"指数 {len(indices)} / 股票 {len(stocks)} / data_date={data_date}")
    return payload


def main():
    build()


if __name__ == "__main__":
    main()
