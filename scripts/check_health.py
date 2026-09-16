#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据健康闸门：挡住「看似成功、实际静默腐烂」的发布。

检查项：
  1) 交易日历剩余天数（<30 红；ensure_calendar 应已续期）
  2) cn10y 相对 last_complete_day 的滞后（>10 自然日红）
  3) 关键指数 parquet 新鲜度（H20269/H30269 等；全收益无备用时收盘后缺当日即红）
  4) weather.json / timing_db.json：锚定指数齐全、data_date 新鲜、无混日（收盘后）
  5) 载荷体积预警（weather+timing 合计 > 3MB 警告）
  6) 可选：股票 hfq 覆盖率（--stock-coverage 时）

用法:
  python3 scripts/check_health.py              # CI 默认硬闸门
  python3 scripts/check_health.py --soft       # 只警告不红
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from fetch_index import last_complete_day  # noqa: E402

WEATHER_ANCHORS = ["000001", "399001", "000300", "399006", "932000"]
# 引擎/门户关键指数：过期则静默用旧信号（比 weather 更隐蔽）
CRITICAL_INDICES = [
    ("H20269", "红利低波全收益"),
    ("H30269", "红利低波价格"),
    ("H00300", "沪深300全收益"),
    ("000300", "沪深300"),
    ("000001", "上证"),
    ("399001", "深成指"),
    ("399006", "创业板指"),
    ("932000", "中证2000"),
]
CN10Y_CSV = os.path.join(C.BASE, "engine", "backtest", "cn10y_daily.csv")
CAL_WARN_DAYS = 60
CAL_FAIL_DAYS = 30
CN10Y_MAX_LAG_DAYS = 10
PAYLOAD_WARN_MB = 3.0
INDEX_MAX_LAG_DAYS = 3  # 相对 last_complete_day；收盘后更严


def _bj_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _as_date(v) -> datetime.date | None:
    if v is None:
        return None
    if isinstance(v, list):
        # 混日：取最小，并标记
        dates = [pd.Timestamp(x).date() for x in v]
        return min(dates) if dates else None
    return pd.Timestamp(v).date()


def check_calendar(errors: list, warnings: list) -> None:
    cal = C.read_df(f"{C.META}/trade_calendar.parquet")
    if cal.empty:
        errors.append("交易日历 parquet 为空")
        return
    end = pd.to_datetime(cal["date"]).max().date()
    remain = (end - _bj_now().date()).days
    print(f"[health] calendar end={end} remain_days={remain}")
    if remain < CAL_FAIL_DAYS:
        errors.append(f"交易日历剩余 {remain} 天（<{CAL_FAIL_DAYS}），"
                      f"请跑 scripts/ensure_calendar.py")
    elif remain < CAL_WARN_DAYS:
        warnings.append(f"交易日历剩余 {remain} 天，建议续期")


def check_cn10y(errors: list, warnings: list) -> None:
    if not os.path.exists(CN10Y_CSV):
        errors.append(f"缺少 {CN10Y_CSV}")
        return
    df = pd.read_csv(CN10Y_CSV)
    col = "date" if "date" in df.columns else df.columns[0]
    last = pd.to_datetime(df[col]).max().date()
    expect = pd.Timestamp(last_complete_day()).date()
    lag = (expect - last).days
    print(f"[health] cn10y last={last} expect≈{expect} lag_days={lag}")
    if lag > CN10Y_MAX_LAG_DAYS:
        errors.append(
            f"cn10y 滞后 {lag} 天（>{CN10Y_MAX_LAG_DAYS}）："
            f"估值门会 ffill 旧收益率而不报警。"
            f"请本地 pip install akshare && python3 engine/refresh_cn10y.py 后提交 CSV"
        )
    elif lag > 5:
        warnings.append(f"cn10y 滞后 {lag} 天，建议尽快刷新")


def check_weather_timing(errors: list, warnings: list) -> None:
    expect = pd.Timestamp(last_complete_day()).date()
    now = _bj_now()
    after_close = now.time() >= datetime.time(16, 0)

    for name, path in (
        ("weather", f"{C.PAYLOAD_DIR}/weather.json"),
        ("timing_db", f"{C.PAYLOAD_DIR}/timing_db.json"),
    ):
        if not os.path.exists(path):
            errors.append(f"缺少 {path}")
            continue
        raw = json.load(open(path, encoding="utf-8"))
        dd = raw.get("data_date")
        mixed = isinstance(dd, list)
        dmin = _as_date(dd)
        size_mb = os.path.getsize(path) / 1e6
        print(f"[health] {name} data_date={dd} size={size_mb:.2f}MB")

        if name == "weather":
            got = set((raw.get("indices") or {}).keys())
            miss = [c for c in WEATHER_ANCHORS if c not in got]
            if miss:
                errors.append(f"weather 缺少锚定指数: {miss}")
            # 各指数 w.date 是否一致
            wdates = sorted({
                (raw["indices"][c].get("w") or {}).get("date")
                for c in got if c in raw.get("indices", {})
            } - {None})
            if len(wdates) > 1:
                msg = f"weather 指数混日: {wdates}"
                if after_close:
                    errors.append(msg + "（收盘后 CSI 应已对齐；拒绝发布）")
                else:
                    warnings.append(msg + "（午间允许，以 min 为准）")

        if name == "timing_db":
            got = set((raw.get("indices") or {}).keys())
            miss = [c for c in WEATHER_ANCHORS if c not in got]
            if miss:
                errors.append(f"timing_db 缺少锚定指数: {miss}")
            if mixed:
                errors.append("timing_db data_date 不应为列表（应对齐 min）")

        if dmin is None:
            errors.append(f"{name} data_date 缺失")
        else:
            lag = (expect - dmin).days
            # 节假日/周末 expect 回退时 lag 可为 0；允许 1 个交易日缓冲（约 3 自然日）
            if lag > 3:
                errors.append(f"{name} data_date={dmin} 落后 expect={expect} ({lag} 天)")
            if mixed and after_close:
                errors.append(f"{name} 收盘后仍混日 data_date={dd}")

    total = 0.0
    for fn in ("weather.json", "timing_db.json"):
        p = f"{C.PAYLOAD_DIR}/{fn}"
        if os.path.exists(p):
            total += os.path.getsize(p) / 1e6
    if total > PAYLOAD_WARN_MB:
        warnings.append(
            f"weather+timing_db 合计 {total:.1f}MB > {PAYLOAD_WARN_MB}MB，"
            f"检查 PAYLOAD_KEEP_YEARS 截断是否生效"
        )


def check_critical_indices(errors: list, warnings: list) -> None:
    """关键指数 parquet 新鲜度：CSI 全收益无备用时，失败会静默沿用昨日 → 必须闸门拦截。"""
    expect = pd.Timestamp(last_complete_day()).date()
    now = _bj_now()
    after_close = now.time() >= datetime.time(16, 0)
    for code, label in CRITICAL_INDICES:
        p = f"{C.INDEX_DIR}/{code}.parquet"
        if not os.path.exists(p):
            errors.append(f"缺少关键指数 {code}({label}): {p}")
            continue
        df = C.read_df(p)
        if df.empty:
            errors.append(f"关键指数 {code}({label}) 空表")
            continue
        last = pd.to_datetime(df["date"]).max().date()
        lag = (expect - last).days
        print(f"[health] index {code} last={last} expect={expect} lag={lag}")
        if lag > INDEX_MAX_LAG_DAYS:
            errors.append(
                f"{code}({label}) 滞后 {lag} 天（last={last} < expect={expect}）；"
                f"CSI/备用通道可能断源"
            )
        elif lag > 0 and after_close:
            # 收盘后仍缺当日：全收益 H* 尤其危险（无 EM/TX 兜底）
            if code in ("H20269", "H00300"):
                errors.append(
                    f"{code}({label}) 收盘后仍缺当日 bar（last={last}）；"
                    f"全收益指数无备用通道，拒绝发布残缺信号"
                )
            else:
                warnings.append(f"{code}({label}) 收盘后缺当日（last={last}）")


def check_stock_coverage(errors: list, warnings: list, min_ratio: float) -> None:
    meta = C.read_df(f"{C.META}/stocks.parquet")
    if meta.empty or "status" not in meta.columns:
        warnings.append("无 stocks.parquet，跳过覆盖率")
        return
    alive = meta[meta["status"] == 1]
    n_alive = len(alive)
    end = last_complete_day()
    try:
        import duckdb
        con = duckdb.connect()
        n_have = con.execute(
            f"""SELECT count(DISTINCT code) FROM read_parquet('{C.STOCK_DIR}/hfq_*.parquet')
                WHERE date = DATE '{end}'"""
        ).fetchone()[0]
    except Exception as e:
        warnings.append(f"股票覆盖率查询失败: {e}")
        return
    ratio = (n_have / n_alive) if n_alive else 0.0
    print(f"[health] stock hfq@{end}: {n_have}/{n_alive} = {ratio:.1%}")
    if ratio < min_ratio:
        warnings.append(
            f"股票 hfq 当日覆盖率 {ratio:.1%} < {min_ratio:.0%}（WAF/断档）；"
            f"选股可能残缺，mirror 主通道需关注"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--soft", action="store_true", help="警告不红")
    ap.add_argument("--stock-coverage", action="store_true")
    ap.add_argument("--min-coverage", type=float, default=0.90)
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    check_calendar(errors, warnings)
    check_cn10y(errors, warnings)
    check_critical_indices(errors, warnings)
    check_weather_timing(errors, warnings)
    if args.stock_coverage:
        check_stock_coverage(errors, warnings, args.min_coverage)

    for w in warnings:
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")

    C.manifest_add({
        "event": "check_health", "at": C.bj_now(),
        "errors": errors, "warnings": warnings,
    })
    if errors and not args.soft:
        print(f"[health] FAIL ({len(errors)} errors)")
        return 1
    print(f"[health] OK (warnings={len(warnings)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
