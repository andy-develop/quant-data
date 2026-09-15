#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性迁移脚本：把既有仓库数据汇总进 quant-data 统一仓库（Parquet）。

输入（环境变量可覆盖，默认本机路径）：
  QLAB_DATA   quant-lab/data —— 股票日K(raw+hfq 分片/增量/修复件) + 股票列表 + 3年指数日线
  RDD_DATA    red-dividend-strategy —— 中证指数归档(H20269/H30269/H00300/000300 周基线+日增量)
              + 行业轮动 ETF 归档(sector-week/incr) + 交易日历

输出：
  data/meta/stocks.parquet       全A股票列表(腾讯式代码)
  data/meta/indices.parquet      指数/ETF 清单与数据范围
  data/meta/trade_calendar.parquet
  data/kline/stock/raw_2024.parquet  股票日K不复权(3年, 含真实成交额, 按年分片)
  data/kline/stock/hfq_2024.parquet  股票日K后复权(3年, 按年分片)
  data/kline/index/<code>.parquet  指数日K（H20269/H30269/H00300/000300/000001/000852）
  data/kline/etf/etf_kline.parquet  ETF日K前复权(东财口径)
用法: python3 scripts/migrate.py
"""
import glob
import json
import gzip
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

QLAB_DATA = os.environ.get("QLAB_DATA",
                           "/Users/andy/WorkBuddy/2026-09-03-14-42-54/quant-lab/data")
RDD_DATA = os.environ.get("RDD_DATA", os.path.join(C.BASE, "..", "red-dividend-strategy"))


# ---------------- 股票K线（quant-lab） ----------------


def fixup_code(fname: str, prefix: str) -> str:
    """修复件文件名 raw_0_000672.parquet / hfq_1_600000.parquet -> 腾讯式代码。"""
    b = os.path.basename(fname)
    assert b.startswith(prefix), b
    return b[len(prefix):].split(".")[0].replace("_", ".")


def load_stock_series(prefix: str) -> pd.DataFrame:
    """合并 分片 + incremental + fixup（fixup 整段覆盖），返回腾讯式代码的完整序列。"""
    shards = sorted(glob.glob(f"{QLAB_DATA}/kline/{prefix}_*.parquet"))
    incs = sorted(glob.glob(f"{QLAB_DATA}/kline/incremental/{prefix}_*.parquet"))
    dfs = []
    for f in shards + incs:
        df = pd.read_parquet(f)
        df["code"] = df["code"].map(C.to_tx_code)
        dfs.append(df)
    store = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
    fixs = sorted(glob.glob(f"{QLAB_DATA}/kline/fixup/{prefix}_*.parquet"))
    if fixs:
        fixes = []
        for f in fixs:
            df = pd.read_parquet(f)
            df["code"] = df["code"].map(C.to_tx_code) if "code" in df.columns else fixup_code(f, f"{prefix}_")
            fixes.append(df)
        fix = pd.concat(fixes, ignore_index=True)
        store = store[~store["code"].isin(set(fix["code"]))]
        store = pd.concat([store, fix], ignore_index=True)
    if store.empty:
        return store
    store["date"] = pd.to_datetime(store["date"])
    return store.drop_duplicates(["code", "date"]).sort_values(["code", "date"]).reset_index(drop=True)


def migrate_stocks() -> None:
    raw = load_stock_series("raw")
    hfq = load_stock_series("hfq")
    print(f"[migrate] 股票K线: raw {len(raw):,} 行 / hfq {len(hfq):,} 行, "
          f"{raw['date'].min().date()} -> {raw['date'].max().date()}" if len(raw) else "[migrate] raw 为空!")
    # 保留策略：股票只留 3 年（回看窗口）
    cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=3)
    if len(raw):
        raw = raw[raw["date"] >= cutoff]
    if len(hfq):
        hfq = hfq[hfq["date"] >= cutoff]
    C.stock_write(raw, "raw", sort=["code", "date"])
    C.stock_write(hfq, "hfq", sort=["code", "date"])
    print(f"[migrate] 股票K线入库(3年保留, cutoff={cutoff.date()}): "
          f"raw {len(raw):,} / hfq {len(hfq):,}")


def migrate_stock_meta() -> None:
    src = f"{QLAB_DATA}/meta/stock_basic.parquet"
    if not os.path.exists(src):
        print("[migrate] 无 stock_basic, 跳过股票列表")
        return
    df = pd.read_parquet(src)
    out = df.copy()
    # stock_basic 同时有 code(baostock 式) 与 secid(腾讯式)：取腾讯式为主代码
    if "secid" in out.columns:
        out["code"] = out["secid"]
    out["code"] = out["code"].map(C.to_tx_code)
    keep = [c for c in ("code", "name", "ipo_date", "out_date", "status") if c in out.columns]
    C.write_df(out[keep], f"{C.META}/stocks.parquet", sort=["code"])
    print(f"[migrate] 股票列表: {len(out)} 只")


# ---------------- 中证指数（red-dividend 归档） ----------------


def _archive_date(fname: str, code: str):
    import datetime
    return datetime.datetime.strptime(
        os.path.basename(fname).split(f"{code}-")[1].split(".")[0].split("-")[-1], "%Y%m%d").date()


def rebuild_csi(code: str) -> pd.DataFrame:
    """周基线全量 + 其后日增量合并（与 red-dividend rebuild_hl 同构），返回 date/close 序列。"""
    rows_map: dict = {}
    weeks = sorted(glob.glob(f"{RDD_DATA}/data/{code}-week-*.json"))
    if weeks:
        d = json.load(open(weeks[-1], encoding="utf-8"))
        rows_map = {r["tradeDate"]: r for r in d["rows"]}
        base_day = _archive_date(weeks[-1], code)
        for incr in sorted(glob.glob(f"{RDD_DATA}/data/{code}-incr-*.json")):
            if _archive_date(incr, code) <= base_day:
                continue
            for r in json.load(open(incr, encoding="utf-8"))["rows"]:
                rows_map[r["tradeDate"]] = r
    if not rows_map:
        for p in sorted(glob.glob(f"{RDD_DATA}/data/{code}-????????.json"), reverse=True):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if d.get("indexCode") == code and d.get("rows"):
                rows_map = {r["tradeDate"]: r for r in d["rows"]}
                break
    if not rows_map:
        return pd.DataFrame()
    df = pd.DataFrame([{"date": pd.to_datetime(r["tradeDate"]),
                        "close": float(r["close"])} for r in rows_map.values()])
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def migrate_indices() -> None:
    rows = []
    # 中证指数归档（13年）
    for code, name in (("H20269", "中证红利低波全收益"), ("H30269", "中证红利低波价格"),
                       ("H00300", "沪深300全收益"), ("000300", "沪深300价格")):
        df = rebuild_csi(code)
        if df.empty:
            print(f"[migrate] {code} 无归档, 跳过")
            continue
        df.insert(0, "code", code)
        C.write_df(df, f"{C.INDEX_DIR}/{code}.parquet", sort=["code", "date"])
        rows.append({"code": code, "name": name, "kind": "index", "secid": "",
                     "start": str(df["date"].min().date()), "end": str(df["date"].max().date()),
                     "n": int(len(df))})
        print(f"[migrate] 指数 {code} {name}: {len(df):,} 行 "
              f"{df['date'].min().date()} -> {df['date'].max().date()}")
    # quant-lab 3年指数日线（上证/沪深300/中证1000 为 000001 打底，000300 有归档则跳过）
    have = {r["code"] for r in rows}
    ql_map = {"index_daily": ("000001", "上证指数"), "bench_daily": ("000300", "沪深300"),
              "csi1000_daily": ("000852", "中证1000")}
    for fname, (code, name) in ql_map.items():
        if code in have:
            continue
        src = f"{QLAB_DATA}/meta/{fname}.parquet"
        if not os.path.exists(src):
            continue
        df = pd.read_parquet(src)
        df["date"] = pd.to_datetime(df["date"])
        df.insert(0, "code", code)
        df = df[["code", "date", "open", "close", "high", "low", "volume", "amount"]]
        C.write_df(df, f"{C.INDEX_DIR}/{code}.parquet", sort=["code", "date"])
        rows.append({"code": code, "name": name, "kind": "index", "secid": "sh" + code,
                     "start": str(df["date"].min().date()), "end": str(df["date"].max().date()),
                     "n": int(len(df))})
        print(f"[migrate] 指数 {code} {name}: {len(df):,} 行 (quant-lab 3年)")
    # 指数清单
    C.write_df(pd.DataFrame(rows), f"{C.META}/indices.parquet", sort=["code"])
    print(f"[migrate] 指数清单: {len(rows)} 个")


# ---------------- ETF 日K（red-dividend sector 归档，东财前复权） ----------------


def migrate_etf() -> None:
    files = (sorted(glob.glob(f"{RDD_DATA}/data/sector-week-*.json.gz"))
             + sorted(glob.glob(f"{RDD_DATA}/data/sector-incr-*.json.gz")))
    recs = {}
    for f in files:
        try:
            d = json.load(gzip.open(f, "rt", encoding="utf-8"))
        except Exception:
            continue
        for code, meta in (d.get("etfs") or {}).items():
            rec = recs.setdefault(code, {"code": code, "industry": meta.get("industry", ""),
                                         "name": meta.get("name", ""), "rows": {}})
            for k in meta.get("klines") or []:
                # kline 为 CSV 字符串: date,open,close,high,low,volume,amount,amplitude,pct,change,turnover
                parts = k.split(",") if isinstance(k, str) else list(k)
                if len(parts) >= 7:
                    rec["rows"][parts[0]] = parts
    if not recs:
        print("[migrate] 无 sector 归档, 跳过 ETF")
        return
    frames = []
    import re as _re
    _date_re = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
    skipped = 0
    for code, rec in recs.items():
        rows = []
        for r in rec["rows"].values():
            if not isinstance(r, (list, tuple)) or len(r) < 7 or not _date_re.match(str(r[0])):
                skipped += 1
                continue
            try:
                rows.append({"date": pd.to_datetime(r[0]), "open": float(r[1]), "close": float(r[2]),
                             "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                             "amount": float(r[6]) if r[6] else 0.0,
                             "turnover": float(r[10]) if len(r) > 10 and r[10] else 0.0})
            except (ValueError, TypeError):
                skipped += 1
        if rows:
            df = pd.DataFrame(rows)
            df.insert(0, "code", code)
            df.insert(1, "name", rec["name"])
            df.insert(2, "industry", rec["industry"])
            frames.append(df)
    if skipped:
        print(f"[migrate] ETF 跳过异常行 {skipped} 条")
    etf = pd.concat(frames, ignore_index=True)
    etf = etf.drop_duplicates(["code", "date"]).sort_values(["code", "date"]).reset_index(drop=True)
    C.write_df(etf, f"{C.ETF_DIR}/etf_kline.parquet", sort=["code", "date"])
    meta = etf.groupby("code").agg(name=("name", "first"), industry=("industry", "first"),
                                   start=("date", "min"), end=("date", "max"), n=("date", "count"))
    meta = meta.reset_index().sort_values("code")
    meta["kind"] = "etf"
    meta["secid"] = meta["code"].map(C.etf_secid)
    C.write_df(meta[["code", "name", "industry", "kind", "secid", "start", "end", "n"]],
               f"{C.META}/etfs.parquet", sort=["code"])
    print(f"[migrate] ETF: {len(meta)} 只 {etf['date'].min().date()} -> {etf['date'].max().date()}, "
          f"{len(etf):,} 行")


# ---------------- 交易日历 ----------------


def migrate_calendar() -> None:
    src = f"{RDD_DATA}/trade_calendar.csv"
    if os.path.exists(src):
        df = pd.read_csv(src, parse_dates=["trade_date"])
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"trade_date": "date"})
        C.write_df(df, f"{C.META}/trade_calendar.parquet", sort=["date"])
        print(f"[migrate] 交易日历: {len(df)} 天 {df['date'].min().date()} -> {df['date'].max().date()}")
    else:
        print("[migrate] 无 trade_calendar.csv, 跳过交易日历")


def main() -> None:
    C.ensure_dirs()
    migrate_stocks()
    migrate_stock_meta()
    migrate_indices()
    migrate_etf()
    migrate_calendar()
    C.manifest_add({"event": "migrate", "at": C.bj_now(),
                    "qlab": os.path.basename(QLAB_DATA), "rdd": os.path.basename(RDD_DATA)})
    print("迁移完成")


if __name__ == "__main__":
    main()
