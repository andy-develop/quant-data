#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从官方接口重新生成回测 CSV（h20269_daily.csv / h30269_daily.csv / cn10y_daily.csv）。
v7.12 修复"三份 CSV 无主产物"：此脚本是 CSV 的唯一合法来源，清洗规则固化在仓库内。

清洗规则：
  1. 剔除接口返回的假日复制行（非交易日重复行）——用交易日历过滤
  2. 统一日期格式 YYYY-MM-DD，升序、去重
  3. close/px 强转 float，剔除 NaN/<=0
用法：python3 prep_data.py [--out-dir .]
"""
import json, os, sys, time, datetime
import urllib.request
import pandas as pd

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
FETCH_START = "20130719"
BASE = os.path.dirname(os.path.abspath(__file__))
CAL = os.path.join(BASE, "trade_calendar.csv")


def fetch_index(code, start, end, retries=4):
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode("utf-8"))
            rows = j.get("data") or []
            if rows:
                return rows
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch {code} failed: {last}")


def clean(rows, code):
    """原始行 -> 清洗后 DataFrame(date/close[/trading_vol/trading_value])。"""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["tradeDate"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[(df["close"] > 0)].copy()
    if "tradingVol" in df.columns:
        df["trading_vol"] = pd.to_numeric(df["tradingVol"], errors="coerce")
    if "tradingValue" in df.columns:
        df["trading_value"] = pd.to_numeric(df["tradingValue"], errors="coerce")
    # 剔除假日复制行：只保留交易日（2013 起有交易日历；日历缺失时按周末过滤）
    if os.path.exists(CAL):
        tdays = set(pd.read_csv(CAL, parse_dates=["trade_date"])["trade_date"].dt.date.tolist())
        df = df[df["date"].dt.date.isin(tdays)]
    else:
        df = df[df["date"].dt.weekday < 5]
    df = df.sort_values("date").drop_duplicates(subset="date").reset_index(drop=True)
    return df


def main():
    out_dir = BASE
    if "--out-dir" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out-dir") + 1]
    today = datetime.date.today().strftime("%Y%m%d")
    for code, fname, keep in (("H20269", "h20269_daily.csv", ["date", "close", "trading_vol", "trading_value"]),
                              ("H30269", "h30269_daily.csv", ["date", "close"])):
        rows = fetch_index(code, FETCH_START, today)
        df = clean(rows, code)
        cols = [c for c in keep if c in df.columns]
        df[cols].to_csv(os.path.join(out_dir, fname), index=False)
        print(f"{fname}: {len(df)} 行, {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    # cn10y 由 refresh_cn10y.py 维护（akshare），此处仅提示
    print("cn10y_daily.csv 请用 refresh_cn10y.py 刷新（akshare 数据源）")


if __name__ == "__main__":
    main()
