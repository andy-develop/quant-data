#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quant-data 数据仓库公共工具：路径、会话、代码格式统一、读写 helper。

数据格式约定（全仓库统一，与 quant-lab 快照口径一致）：
  - 股票代码: 腾讯式 "1.600000"(沪) / "0.000001"(深)
  - 指数代码: 短码 "000001" + 市场标识存 indices.parquet(secid 如 sh000001)
  - 日期列: datetime64[ns]（pandas 原生，DuckDB 可直接分析）
  - 复权: raw(不复权, 含真实成交额) + hfq(后复权, 信号复现口径)，与 quant-lab 同源
"""
import glob
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = f"{BASE}/data"
META = f"{DATA}/meta"
KDIR = f"{DATA}/kline"
STOCK_DIR = f"{KDIR}/stock"
INDEX_DIR = f"{KDIR}/index"
ETF_DIR = f"{KDIR}/etf"
INCR_DIR = f"{KDIR}/incremental"
SNAP_DIR = f"{DATA}/snapshot"
FACTOR_DIR = f"{DATA}/factors"
PAYLOAD_DIR = f"{DATA}/payload"
TMP = f"{DATA}/.tmp"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------- 路径 ----------------


def ensure_dirs():
    for d in (DATA, META, STOCK_DIR, INDEX_DIR, ETF_DIR, INCR_DIR, SNAP_DIR,
              FACTOR_DIR, PAYLOAD_DIR, TMP):
        os.makedirs(d, exist_ok=True)


def path(*parts) -> str:
    return os.path.join(DATA, *parts)


# ---------------- 读写 helper ----------------


def read_df(p: str) -> pd.DataFrame:
    if not os.path.exists(p):
        return pd.DataFrame()
    return pd.read_parquet(p)


def write_df(df: pd.DataFrame, p: str, sort=None) -> None:
    """写 parquet；sort 为排序列时先去重排序再写，保证可复现。"""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if sort:
        cols = [c for c in sort if c in df.columns]
        if cols:
            df = df.drop_duplicates(cols).sort_values(cols).reset_index(drop=True)
    tmp = p + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, p)


def stock_read(prefix: str) -> pd.DataFrame:
    """读股票序列：年份分片 {prefix}_YYYY.parquet + 日增量 {prefix}_incr_YYYYMMDD.parquet
    合并（增量在后，同日重复以增量为准），按 code/date 排序。空则空表。"""
    files = sorted(glob.glob(f"{STOCK_DIR}/{prefix}_*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return (df.drop_duplicates(["code", "date"], keep="last")
              .sort_values(["code", "date"]).reset_index(drop=True))


def stock_write(df: pd.DataFrame, prefix: str, sort=None) -> None:
    """compact：按年份分片写 {prefix}_YYYY.parquet（单片 <50MB，适配 GitHub 软限）；
    删除不再需要的年份文件与全部 {prefix}_incr_*.parquet 增量文件（已并入）。
    滚动保留由调用方先过滤 df。"""
    if df.empty:
        return
    df = df.copy()
    df["_y"] = df["date"].dt.year
    keep = set()
    for y, sub in df.groupby("_y"):
        keep.add(y)
        write_df(sub.drop(columns="_y"), f"{STOCK_DIR}/{prefix}_{y}.parquet", sort=sort)
    for p in glob.glob(f"{STOCK_DIR}/{prefix}_*.parquet"):
        base = os.path.basename(p)
        if "_incr_" in base:
            os.remove(p)
            print(f"  清理增量(已并入分片): {base}")
            continue
        try:
            y = int(base[len(prefix) + 1:base.index(".parquet")])
        except ValueError:
            continue
        if y not in keep:
            os.remove(p)


def stock_incr_write(df: pd.DataFrame, prefix: str) -> None:
    """日增量写入 {prefix}_incr_YYYYMMDD.parquet（按 df 内最大日期命名，同日幂等追加）。"""
    if df.empty:
        return
    df = df.copy()
    day = df["date"].max().strftime("%Y%m%d")
    p = f"{STOCK_DIR}/{prefix}_incr_{day}.parquet"
    old = pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame()
    merged = pd.concat([df, old], ignore_index=True).drop_duplicates(["code", "date"])
    write_df(merged, p, sort=["code", "date"])
    print(f"  {prefix} 增量 -> {os.path.basename(p)} ({len(merged):,} 行)")


# ---------------- 代码格式 ----------------


def to_tx_code(code: str) -> str:
    """baostock 式(sh./sz.) -> 腾讯式(1./0.)；已腾讯式原样返回。"""
    code = str(code)
    if code.startswith("sh."):
        return "1." + code[3:]
    if code.startswith("sz."):
        return "0." + code[3:]
    return code


def tx_to_symbol(code: str) -> str:
    """腾讯式(1.600000) -> 腾讯行情符号(sh600000)。"""
    mkt, num = code.split(".")
    return ("sh" if mkt == "1" else "sz") + num


def etf_secid(code: str) -> str:
    """ETF 场内代码 -> 东财 secid（5/6 开头沪 1.，其余深 0.）。"""
    return ("1." if code.startswith(("5", "6")) else "0.") + code


def bj_now() -> str:
    import datetime
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M"))


def manifest_add(entry: dict) -> None:
    """把一次运行的关键信息追加到 data/meta/manifest.jsonl（可复现审计）。"""
    import json
    ensure_dirs()
    p = f"{META}/manifest.jsonl"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
