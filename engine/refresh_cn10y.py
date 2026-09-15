#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新十年期国债收益率静态缓存（cn10y_daily.csv）。
国债收益率是慢变量，但长期不刷新会让估值剪刀差分位逐步漂移——
建议每季度手动跑一次（或加到计划任务）：python3 refresh_cn10y.py

数据源：akshare bond_zh_us_rate（东财/英为财情聚合，2013 起日频）。
依赖：pip install akshare（仅刷新时需要，每日 Actions 流水线不装）。
"""
import os, sys
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "cn10y_daily.csv")

def main():
    try:
        import akshare as ak
    except ImportError:
        print("需要先安装 akshare：pip install akshare", file=sys.stderr)
        return 1
    print("拉取十年期国债收益率（2013-01 起）...")
    df = ak.bond_zh_us_rate(start_date="20130101")
    out = df[["日期", "中国国债收益率10年"]].rename(columns={"日期": "date", "中国国债收益率10年": "y10"})
    out = out.dropna(subset=["y10"]).sort_values("date")
    out.to_csv(OUT, index=False)
    print(f"已写入 {OUT}: {len(out)} 行, {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}")
    print(f"最新十年期国债收益率: {out['y10'].iloc[-1]:.4f}%")
    return 0

if __name__ == "__main__":
    sys.exit(main())
