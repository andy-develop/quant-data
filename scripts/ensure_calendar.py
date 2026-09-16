#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易日历续期与闸门。

问题：data/meta/trade_calendar.parquet 与 engine/trade_calendar.csv 若只到某年底，
last_complete_day() 会永久卡在末日，指数/股票/ETF 增量全部空转却看似成功。

本脚本：
  1) 若日历末距今不足 MIN_REMAINING_DAYS 个自然日，则向后追加「周一到周五」
     作为临时交易日（不含节假日剔除；宁可多一天空增量，不可冻结全市场）。
  2) 同步写入 parquet（列 date）与 engine/trade_calendar.csv（列 trade_date）。
  3) 若日历已过期（末日 < 今天）则非 0 退出，阻断 CI。

用法: python3 scripts/ensure_calendar.py [--years 2]
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

MIN_REMAINING_DAYS = 90
ENGINE_CSV = os.path.join(C.BASE, "engine", "trade_calendar.csv")
PARQUET = f"{C.META}/trade_calendar.parquet"


def _bj_today() -> datetime.date:
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).date()


def _load() -> pd.DataFrame:
    if os.path.exists(PARQUET):
        df = C.read_df(PARQUET)
        if len(df) and "date" in df.columns:
            out = df[["date"]].copy()
            out["date"] = pd.to_datetime(out["date"]).dt.normalize()
            return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    if os.path.exists(ENGINE_CSV):
        df = pd.read_csv(ENGINE_CSV)
        col = "trade_date" if "trade_date" in df.columns else "date"
        out = pd.DataFrame({"date": pd.to_datetime(df[col]).dt.normalize()})
        return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return pd.DataFrame(columns=["date"])


def _extend_weekdays(df: pd.DataFrame, years: int) -> pd.DataFrame:
    """从现有末日次日追加 years 年的周一~周五。"""
    if df.empty:
        start = datetime.date(_bj_today().year, 1, 1)
    else:
        start = (df["date"].max() + pd.Timedelta(days=1)).date()
    end = start + datetime.timedelta(days=365 * years + 30)
    days = pd.bdate_range(start=start, end=end, freq="C")  # 周一~周五
    add = pd.DataFrame({"date": pd.to_datetime(days)})
    if add.empty:
        return df
    out = pd.concat([df, add], ignore_index=True)
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _save(df: pd.DataFrame) -> None:
    C.ensure_dirs()
    C.write_df(df, PARQUET, sort=["date"])
    os.makedirs(os.path.dirname(ENGINE_CSV), exist_ok=True)
    csv = pd.DataFrame({"trade_date": df["date"].dt.strftime("%Y-%m-%d")})
    csv.to_csv(ENGINE_CSV, index=False)
    print(f"[ensure_calendar] → {PARQUET} / {ENGINE_CSV} "
          f"({df['date'].min().date()} → {df['date'].max().date()}, n={len(df)})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2, help="续期年数（默认 2）")
    args = ap.parse_args()

    today = _bj_today()
    df = _load()
    if df.empty:
        print("[ensure_calendar] 错误: 无现有交易日历可续期", file=sys.stderr)
        return 1

    cal_end = df["date"].max().date()
    remain = (cal_end - today).days
    print(f"[ensure_calendar] 当前末日 {cal_end}，距今 {remain} 自然日")

    if remain < 0:
        print(f"[ensure_calendar] 错误: 日历已过期 { -remain } 天，"
              f"增量取数会冻结在 {cal_end}", file=sys.stderr)
        # 仍尝试续期，但返回非 0 让 CI 红，迫使确认节假日
        df = _extend_weekdays(df, args.years)
        _save(df)
        C.manifest_add({"event": "ensure_calendar", "at": C.bj_now(),
                        "status": "expired_extended", "end": str(df["date"].max().date())})
        return 1

    if remain < MIN_REMAINING_DAYS:
        print(f"[ensure_calendar] 剩余 < {MIN_REMAINING_DAYS} 天，追加 {args.years} 年工作日…")
        before = len(df)
        df = _extend_weekdays(df, args.years)
        _save(df)
        print(f"[ensure_calendar] +{len(df) - before} 行（工作日临时续期，节假日未剔除）")
        C.manifest_add({"event": "ensure_calendar", "at": C.bj_now(),
                        "status": "extended", "added": len(df) - before,
                        "end": str(df["date"].max().date())})
    else:
        # 仍同步一份，防止 parquet/csv 漂移
        _save(df)
        C.manifest_add({"event": "ensure_calendar", "at": C.bj_now(),
                        "status": "ok", "end": str(cal_end), "remain_days": remain})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
