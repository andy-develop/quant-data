#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库维护：滚动保留 + 体积控制（GitHub 仓库体积可控）。

规则（与数据契约一致）：
  - 股票 K 线（raw/hfq）：只保留最近 3 年（数据末日 - 3 年），滚动删除
  - 指数 K 线：只保留最近 10 年
  - ETF K 线：全量保留（32 只体积小，行业轮动需长历史）
  - 上午快照 snapshot/etf_*.parquet：只保留最近 7 天文件
  - 清理 data/.tmp/
  - 输出 data/ 体积报告（GitHub 1GB 软限预警）

注意：因子层 data/factors/ 是确定性中间产物（由 kline 重算），不入库（.gitignore），
不在此清理（build_factors 每次全量重建）。
用法: python3 scripts/housekeeping.py
"""
import glob
import os

import pandas as pd

import common as C

STOCK_KEEP_DAYS = 3 * 365      # 股票 3 年
INDEX_KEEP_DAYS = 13 * 365     # 指数全量保留（原始 13 年，满足"≥10 年"且保住引擎预热：
                               #   build_signals 的 div_proxy 需 shift(252)、spread_pct 需
                               #   rolling(756,min_periods=604)，截断会破坏 2016-09-08 起回测口径）
SNAP_KEEP_FILES = 7            # 快照保留最近 7 天


def prune(df: pd.DataFrame, keep_days: int, sort_cols) -> pd.DataFrame:
    """按数据末日 - keep_days 截断；返回修剪后的 df（行数变化才写回）。"""
    if df.empty:
        return df
    cutoff = df["date"].max() - pd.Timedelta(days=keep_days)
    before = len(df)
    out = df[df["date"] >= cutoff]
    if len(out) < before:
        out = out.reset_index(drop=True)
        print(f"  prune: {before:,} → {len(out):,} 行（cutoff {cutoff.date()}）")
    return out


def main() -> None:
    C.ensure_dirs()

    # 1) 股票 raw/hfq：3 年（按年份分片存储，分片合并修剪后整表回写，自动删过期年份）
    for f in ("raw", "hfq"):
        df = C.stock_read(f)
        if df.empty:
            continue
        out = prune(df, STOCK_KEEP_DAYS, ["code", "date"])
        if len(out) < len(df):
            C.stock_write(out, f, sort=["code", "date"])

    # 2) 指数：10 年（只修剪 kline/index/ 下的 *.parquet）
    for p in glob.glob(f"{C.INDEX_DIR}/*.parquet"):
        df = C.read_df(p)
        out = prune(df, INDEX_KEEP_DAYS, ["date"])
        if len(out) < len(df):
            C.write_df(out, p, sort=["date"])

    # 3) 快照：只保留最近 N 个文件
    snaps = sorted(glob.glob(f"{C.SNAP_DIR}/etf_*.parquet"))
    old = snaps[:-SNAP_KEEP_FILES] if len(snaps) > SNAP_KEEP_FILES else []
    for p in old:
        os.remove(p)
        print(f"  删除旧快照: {os.path.basename(p)}")
    for p in glob.glob(f"{C.SNAP_DIR}/index_*.parquet"):
        day = os.path.basename(p).replace("index_", "").replace(".parquet", "")
        if not any(day in s for s in snaps):
            os.remove(p)
            print(f"  删除孤立指数快照: {os.path.basename(p)}")

    # 4) 清理临时目录
    for p in glob.glob(f"{C.TMP}/*"):
        os.remove(p)
        print(f"  清理 tmp: {os.path.basename(p)}")

    # 5) 体积报告
    total = 0
    print("\n[housekeeping] data/ 体积（GB 软限 1GB）:")
    for sub in sorted(os.listdir(C.DATA)):
        d = f"{C.DATA}/{sub}"
        if not os.path.isdir(d):
            continue
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _, fs in os.walk(d) for f in fs)
        total += size
        print(f"  {sub:12s} {size/1024/1024:8.1f} MB")
    print(f"  {'TOTAL':12s} {total/1024/1024:8.1f} MB")
    if total > 0.9 * 1024 ** 3:
        print("  ⚠ 超过 1GB 软限，需扩大保留期裁剪或迁移冷数据！")
    C.manifest_add({"event": "housekeeping", "at": C.bj_now(),
                    "data_mb": round(total / 1024 / 1024, 1)})


if __name__ == "__main__":
    main()
