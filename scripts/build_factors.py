#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子层：从 K线 parquet 计算日度技术因子（DuckDB 向量化），输出 data/factors/。
  - factors/etf.parquet    32 只 ETF（含 name/industry）
  - factors/index.parquet  7 个指数（10 年，含 name）
  - factors/stock.parquet  全 A 股（3 年，紧凑列 + float32 控体积）

因子口径（与 ETF 引擎一致的公共部分）：
  - 动量 ret_N：close / close.shift(N) - 1（N=5/20/60/120/250）
  - 均线 ma_N（5/10/20/60/120/250）
  - RSI14（SMA 近似：14 日涨/跌均值）
  - vol20：20 日日收益 std × sqrt(252) 年化
  - vol_ratio20：量比 = volume / 20日均量
  - dist_hi250 / dist_lo250：距 250 日高低点距离
  - boll_up / boll_low：布林上/下轨（ma20 ± 2σ，ETF/指数表）

用法: python3 scripts/build_factors.py
"""
import glob
import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

_SQL = """
WITH base AS (
  SELECT code, date, close, volume,
         close - lag(close) OVER w AS dlt,
         close / lag(close) OVER w - 1 AS ret1
  FROM read_parquet(?, union_by_name = true)
  WINDOW w AS (PARTITION BY code ORDER BY date)
), rsi AS (
  SELECT code, date, close, volume, ret1,
         avg(CASE WHEN dlt > 0 THEN dlt ELSE 0 END)
           OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS ag,
         avg(CASE WHEN dlt <= 0 THEN -dlt ELSE 0 END)
           OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS al
  FROM base
), calc AS (
  SELECT code, date, close, ret1,
    close / lag(close, 5)  OVER w - 1 AS ret_5,
    close / lag(close, 20) OVER w - 1 AS ret_20,
    close / lag(close, 60) OVER w - 1 AS ret_60,
    close / lag(close, 120) OVER w - 1 AS ret_120,
    close / lag(close, 250) OVER w - 1 AS ret_250,
    avg(close) OVER w4   AS ma5,
    avg(close) OVER w9   AS ma10,
    avg(close) OVER w19  AS ma20,
    avg(close) OVER w59  AS ma60,
    avg(close) OVER w119 AS ma120,
    avg(close) OVER w249 AS ma250,
    100 - 100 / (1 + ag / NULLIF(al, 0)) AS rsi14,
    stddev_pop(ret1) OVER w19 * sqrt(252) AS vol20,
    volume / NULLIF(avg(volume) OVER w19, 0) AS vol_ratio20,
    close / max(close) OVER w249 - 1 AS dist_hi250,
    close / min(close) OVER w249 - 1 AS dist_lo250
  FROM rsi
  WINDOW w AS (PARTITION BY code ORDER BY date),
         w4 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 4  PRECEDING AND CURRENT ROW),
         w9 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 9  PRECEDING AND CURRENT ROW),
         w19 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
         w59 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
         w119 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 119 PRECEDING AND CURRENT ROW),
         w249 AS (PARTITION BY code ORDER BY date ROWS BETWEEN 249 PRECEDING AND CURRENT ROW)
)
SELECT code, date, close,
       ret_5, ret_20, ret_60, ret_120, ret_250,
       ma5, ma10, ma20, ma60, ma120, ma250,
       rsi14, vol20, vol_ratio20, dist_hi250, dist_lo250
FROM calc
WHERE date >= ?
ORDER BY code, date
"""

_SQL_BOLL = """
SELECT code, date,
       ma20 + 2 * stddev_pop(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS boll_up,
       ma20 - 2 * stddev_pop(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS boll_low
FROM (
  SELECT code, date, close,
         avg(close) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20
  FROM read_parquet(?, union_by_name = true)
)
ORDER BY code, date
"""


def _write_factor_table(con: duckdb.DuckDBPyConnection, src: str, out: str, min_date: str,
                        no_volume: bool = False) -> int:
    if no_volume:
        # hfq 等无 volume 列：视图补 NULL volume，量比列留空由调用方合并 raw
        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW _f_src AS
            SELECT code, date, close, CAST(NULL AS DOUBLE) AS volume
            FROM read_parquet('{src}', union_by_name = true)
        """)
        from_clause, args = "FROM _f_src", [min_date]
    else:
        from_clause, args = "FROM read_parquet(?, union_by_name = true)", [src, min_date]
    sql = _SQL.replace("FROM read_parquet(?, union_by_name = true)", from_clause, 1)
    con.execute(f"DROP TABLE IF EXISTS _f")
    con.execute(f"CREATE TABLE _f AS {sql}", args)
    n = con.execute("SELECT count(*) FROM _f").fetchone()[0]
    # 数值列 float32 控体积（股票 3y 约 380 万行）
    con.execute(f"""
        COPY (
          SELECT code,
                 CAST(date AS TIMESTAMP) AS date,
                 CAST(close AS REAL) AS close,
                 CAST(ret_5 AS REAL) AS ret_5, CAST(ret_20 AS REAL) AS ret_20,
                 CAST(ret_60 AS REAL) AS ret_60, CAST(ret_120 AS REAL) AS ret_120,
                 CAST(ret_250 AS REAL) AS ret_250,
                 CAST(ma5 AS REAL) AS ma5, CAST(ma10 AS REAL) AS ma10,
                 CAST(ma20 AS REAL) AS ma20, CAST(ma60 AS REAL) AS ma60,
                 CAST(ma120 AS REAL) AS ma120, CAST(ma250 AS REAL) AS ma250,
                 CAST(rsi14 AS REAL) AS rsi14, CAST(vol20 AS REAL) AS vol20,
                 CAST(vol_ratio20 AS REAL) AS vol_ratio20,
                 CAST(dist_hi250 AS REAL) AS dist_hi250, CAST(dist_lo250 AS REAL) AS dist_lo250
          FROM _f
        ) TO '{out}' (FORMAT PARQUET, CODEC SNAPPY)
    """)
    print(f"[factors] {os.path.basename(out)}: {n:,} 行")
    return n


def main() -> None:
    C.ensure_dirs()
    con = duckdb.connect()
    min3y = (pd.Timestamp.now() - pd.Timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    min10y = (pd.Timestamp.now() - pd.Timedelta(days=365 * 10)).strftime("%Y-%m-%d")
    fdir = C.FACTOR_DIR

    # ---- ETF（全历史，用于行业轮动动量与布林） ----
    _write_factor_table(con, f"{C.ETF_DIR}/*.parquet", f"{fdir}/etf.parquet", "2000-01-01")
    con.execute(f"""
        COPY (SELECT f.*, m.name, m.industry
              FROM read_parquet('{fdir}/etf.parquet') f
              LEFT JOIN read_parquet('{C.META}/etfs.parquet') m USING (code))
        TO '{fdir}/etf.parquet' (FORMAT PARQUET, CODEC SNAPPY)""")
    boll = con.execute(_SQL_BOLL, [f"{C.ETF_DIR}/*.parquet"]).df()
    if len(boll):
        C.write_df(boll, f"{fdir}/etf_boll.parquet", sort=["code", "date"])
    print("[factors] etf.parquet + etf_boll.parquet 完成")

    # ---- 指数（10 年） ----
    _write_factor_table(con, src_index(), f"{fdir}/index.parquet", min10y)
    con.execute(f"""
        COPY (SELECT f.*, COALESCE(m.name, f.code) AS name
              FROM read_parquet('{fdir}/index.parquet') f
              LEFT JOIN read_parquet('{C.META}/indices.parquet') m USING (code))
        TO '{fdir}/index.parquet' (FORMAT PARQUET, CODEC SNAPPY)""")
    boll = con.execute(_SQL_BOLL, [src_index()]).df()
    if len(boll):
        C.write_df(boll, f"{fdir}/index_boll.parquet", sort=["code", "date"])
    print("[factors] index.parquet + index_boll.parquet 完成")

    # ---- 股票（3 年）：价格因子用 hfq（复权口径），量能因子补 raw ----
    # 股票 K 线按年份分片（raw_2024.parquet 等），DuckDB 用 glob 一次读入
    stock_p = f"{fdir}/stock.parquet"
    n_stock = _write_factor_table(con, f"{C.STOCK_DIR}/hfq_*.parquet", stock_p, min3y, no_volume=True)
    vr = con.execute("""
        SELECT code, date,
               volume / NULLIF(avg(volume) OVER (PARTITION BY code ORDER BY date
                                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) AS vol_ratio20
        FROM read_parquet(?, union_by_name = true)
        WHERE date >= ?
        ORDER BY code, date
    """, [f"{C.STOCK_DIR}/raw_*.parquet", min3y]).df()
    if len(vr):
        stock = C.read_df(stock_p)
        stock = stock.drop(columns=["vol_ratio20"]).merge(vr, on=["code", "date"], how="left")
        C.write_df(stock, stock_p, sort=["code", "date"])
        print(f"[factors] stock.parquet 已并入 raw 量比（{len(vr):,} 行）")
    print(f"[factors] 完成: 股票 {n_stock:,} 行因子（3 年）")
    C.manifest_add({"event": "build_factors", "at": C.bj_now(), "stock_rows": int(n_stock)})


def src_index() -> str:
    """index 因子来源：INDEX_DIR 全部 parquet（每文件已含 code 列）。"""
    return f"{C.INDEX_DIR}/*.parquet"


if __name__ == "__main__":
    main()
