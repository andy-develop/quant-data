#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新十年期国债收益率静态缓存（engine/backtest/cn10y_daily.csv）。

国债收益率是慢变量，但长期不刷新会让估值剪刀差分位逐步漂移。
健康闸门 check_health 滞后 >10 自然日会红；建议每周或 CI 软刷新。

数据源优先级：
  1) 东财 HTTP（datacenter RPTA_WEB_TREASURYYIELD）—— 无额外依赖，CI 可用
  2) akshare.bond_zh_us_rate —— 本地可选回退

用法: python3 engine/refresh_cn10y.py
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "backtest", "cn10y_daily.csv")
EM_URL = "https://datacenter.eastmoney.com/api/data/get"
EM_TOKEN = "894050c76af8597a853f5b408b759f5d"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _fetch_em(start_date: str = "20130101") -> pd.DataFrame:
    """东财中美国债收益率（与 akshare.bond_zh_us_rate 同源）。"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Referer": "https://data.eastmoney.com/cjsj/zmgzsyl.html"})
    params = {
        "type": "RPTA_WEB_TREASURYYIELD",
        "sty": "ALL",
        "st": "SOLAR_DATE",
        "sr": "-1",
        "token": EM_TOKEN,
        "p": "1",
        "ps": "500",
        "pageNo": "1",
        "pageNum": "1",
    }
    r = s.get(EM_URL, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    result = j.get("result") or {}
    pages = int(result.get("pages") or 1)
    frames = [pd.DataFrame(result.get("data") or [])]
    start_ts = pd.Timestamp(start_date)
    for page in range(2, pages + 1):
        params = {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "token": EM_TOKEN,
            "p": str(page),
            "ps": "500",
            "pageNo": str(page),
            "pageNum": str(page),
        }
        last = None
        for k in range(4):
            try:
                r = s.get(EM_URL, params=params, timeout=30)
                r.raise_for_status()
                rows = ((r.json() or {}).get("result") or {}).get("data") or []
                frames.append(pd.DataFrame(rows))
                last = None
                break
            except Exception as e:
                last = e
                time.sleep(1.0 + k)
        if last is not None:
            raise RuntimeError(f"EM treasury page {page} failed: {last}")
        big = pd.concat(frames, ignore_index=True)
        if "SOLAR_DATE" in big.columns and not big.empty:
            oldest = pd.to_datetime(big["SOLAR_DATE"]).min()
            if oldest <= start_ts:
                break
        time.sleep(0.25)
    big = pd.concat(frames, ignore_index=True)
    if big.empty or "EMM00166466" not in big.columns:
        raise RuntimeError("EM treasury empty or missing EMM00166466 (中国国债10年)")
    out = big[["SOLAR_DATE", "EMM00166466"]].rename(
        columns={"SOLAR_DATE": "date", "EMM00166466": "y10"}
    )
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out["y10"] = pd.to_numeric(out["y10"], errors="coerce")
    out = out.dropna(subset=["y10"]).drop_duplicates("date").sort_values("date")
    out = out[out["date"] >= pd.Timestamp(start_date).strftime("%Y-%m-%d")]
    return out.reset_index(drop=True)


def _fetch_akshare(start_date: str = "20130101") -> pd.DataFrame:
    import akshare as ak
    df = ak.bond_zh_us_rate(start_date=start_date)
    out = df[["日期", "中国国债收益率10年"]].rename(
        columns={"日期": "date", "中国国债收益率10年": "y10"}
    )
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["y10"]).sort_values("date")
    return out.reset_index(drop=True)


def main() -> int:
    print("拉取十年期国债收益率（2013-01 起）...")
    out = None
    err_em = None
    try:
        out = _fetch_em("20130101")
        print(f"  数据源: 东财 HTTP ({len(out)} 行)")
    except Exception as e:
        err_em = e
        print(f"  东财 HTTP 失败: {e}")
    if out is None:
        try:
            out = _fetch_akshare("20130101")
            print(f"  数据源: akshare ({len(out)} 行)")
        except ImportError:
            print("需要东财可达，或 pip install akshare 作为回退", file=sys.stderr)
            if err_em:
                print(f"东财错误: {err_em}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"akshare 回退失败: {e}", file=sys.stderr)
            return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"已写入 {OUT}: {len(out)} 行, {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}")
    print(f"最新十年期国债收益率: {out['y10'].iloc[-1]:.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
