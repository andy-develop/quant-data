#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数日K 增量更新：
  1) 中证指数官网（csindex index-perf）：H20269/H30269/H00300/000300/932000/H20782/930782 —— 当日值收盘后发布，
     盘中拉取只能拿到 T-1（12:00 门户任务用 T-1 值，符合"策略信号基于完整收盘"口径）。
  2) CSI 断源兜底：东财 push2his（H30269/000300/932000/930782）→ 腾讯 fqkline（000300）；
     全收益 H20269/H00300/H20782 无等价通道，靠 check_health 拦截过期发布。
  3) 腾讯 fqkline：000001/000905/000852/399001/399006 —— 主机池故障转移；
     按 last_complete_day 截止清洗盘中半截 bar。

用法: python3 scripts/fetch_index.py [--backfill]   # --backfill 仅拉全量历史(迁移用)
"""
import datetime
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

UA = {"User-Agent": C.UA, "Referer": "https://www.csindex.com.cn/"}

CSI_CODES = ["H20269", "H30269", "H00300", "000300", "932000", "H20782", "930782"]
# 价格指数 CSI 断源/滞后时的腾讯兜底（全收益 H20269/H00300/H20782 无等价通道，仍仅 CSI）
CSI_TX_FALLBACK = {
    "000300": "sh000300",
    # 932000（中证2000）腾讯无稳定符号；勿用 sz399303（国证2000）冒充
}
# 东财 push2his 兜底（CSI 官网失败时）：价格指数有行情；全收益无
CSI_EM_FALLBACK = {
    "H30269": "2.H30269",
    "000300": "1.000300",
    "932000": "2.932000",
    "930782": "2.930782",  # 中证500行业中性低波动（500SNLV）价格
}
TX_CODES = {  # 主要指数(10年) — 腾讯 fqkline 主通道（东财对 CI IP 连接级限流，见 red-dividend 台账）
    "000001": ("sh000001", "上证指数"),
    "000905": ("sh000905", "中证500"),
    "000852": ("sh000852", "中证1000"),
    "399001": ("sz399001", "深证成指"),   # 大盘天气 · 择时锚定指数
    "399006": ("sz399006", "创业板指"),   # 大盘天气 · 择时锚定指数
}
# 932000（中证2000）为 93 开头自编指数，仅中证官网提供（CSI 通道）
NEW_INDEX_META = [  # 新增指数清单行（缺失时由 ensure_index_meta 补入）
    {"code": "399001", "name": "深证成指", "kind": "index", "secid": "sz399001"},
    {"code": "399006", "name": "创业板指", "kind": "index", "secid": "sz399006"},
    {"code": "932000", "name": "中证2000", "kind": "index", "secid": ""},
    {"code": "H20782", "name": "中证500行业中性低波动全收益", "kind": "index", "secid": ""},
    {"code": "930782", "name": "中证500行业中性低波动", "kind": "index", "secid": "2.930782"},
]
TX_BACKFILL_START = "2016-01-01"  # 10 年
TX_CHUNK = 2000                   # 腾讯单次最大 bar 数（实测 2000 可一次返回）


def last_complete_day() -> str:
    """当前可用的"最后一个完整交易日"（YYYY-MM-DD）：
    收盘后(>=15:30 北京)取今天，否则取上一交易日；假日自动回退（用交易日历）。

    若日历末日已过期（今天 > max(cal)），抛错阻断——避免永远卡在末日导致空增量假成功。
    """
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    d = now.date()
    cal = C.read_df(f"{C.META}/trade_calendar.parquet")
    if len(cal):
        cal_dates = pd.to_datetime(cal["date"]).dt.date
        days = set(cal_dates)
        cal_end = cal_dates.max()
        if d > cal_end:
            raise RuntimeError(
                f"交易日历已过期（末日 {cal_end} < 今天 {d}）。"
                f"请运行: python3 scripts/ensure_calendar.py"
            )
    else:
        days = None
    if now.time() >= datetime.time(15, 30) and (days is None or d in days):
        return d.isoformat()
    for _ in range(14):
        d -= datetime.timedelta(days=1)
        if days is None or d in days:
            return d.isoformat()
    return d.isoformat()


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(UA)
    return s


def fetch_csi(s: requests.Session, code: str, start: str, end: str) -> list:
    """csindex index-perf 增量区间；空区间(节假日/未发布)也合法返回。"""
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(5):
        try:
            r = s.get(url, timeout=30)
            j = r.json()
            rows = j.get("data") or []
            if rows or start != "20130719":
                return rows
            last = ValueError("empty rows")
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch CSI {code} failed: {last}")


def fetch_tx_kline(s: requests.Session, sym: str, start: str, end: str) -> list:
    """腾讯指数日K：主机池故障转移（proxy → ifzq → web.ifzq）；
    单次最多 2000 根，从 end 向前分页直到覆盖 start。
    返回 bars: [date,open,close,high,low,volume]，按日期升序。"""
    out: dict[str, list] = {}
    e = end
    while True:
        bars = C.tx_fqkline_get(s, sym, start, e, chunk=TX_CHUNK)
        if not bars:
            break
        for b in bars:
            out[b[0]] = b
        first = bars[0][0]
        if first <= start:
            break
        e = (pd.Timestamp(first) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.4)
    return [out[k] for k in sorted(out) if k >= start]


def fetch_em_kline(s: requests.Session, secid: str, start: str, end: str) -> list:
    """东财指数日K（push2his，klt=101 fqt=0 不复权）。
    返回 CSV 行列表；空/失败返回 []。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt=101&fqt=0&beg={start}&end={end}")
    last = None
    for k in range(4):
        try:
            r = s.get(url, headers={"User-Agent": C.UA, "Referer": "https://quote.eastmoney.com/"},
                      timeout=30)
            d = (r.json() or {}).get("data") or {}
            kl = d.get("klines") or []
            if kl:
                return kl
            last = ValueError("empty klines")
        except Exception as e:
            last = e
        time.sleep(1.5 + k)
    if last:
        print(f"[fetch_index] EM {secid} 失败: {last}")
    return []


def _em_to_df(code: str, klines: list) -> pd.DataFrame:
    """东财 klines 'date,o,c,h,l,v,amount' → DataFrame。"""
    rows = []
    for k in klines:
        parts = k.split(",") if isinstance(k, str) else list(k)
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "date": pd.to_datetime(parts[0]),
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except (ValueError, TypeError, IndexError):
            continue
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "code", code)
    return df


def _fill_from_em(s: requests.Session, code: str, secid: str, old: pd.DataFrame) -> int:
    """CSI 无增量时，用东财补齐至 last_complete_day。"""
    p = f"{C.INDEX_DIR}/{code}.parquet"
    last = old["date"].max() if len(old) else pd.Timestamp("2013-07-19")
    cutoff = pd.Timestamp(last_complete_day())
    if len(old) and last >= cutoff:
        return len(old)
    start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = cutoff.strftime("%Y%m%d")
    kl = fetch_em_kline(s, secid, start, end)
    new = _em_to_df(code, kl)
    if new.empty:
        return len(old)
    new = new[(new["date"] > last) & (new["date"] <= cutoff)]
    if new.empty:
        return len(old)
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    C.write_df(df, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: EM兜底 +{len(new)} 行 -> {len(df)} 行 "
          f"({df['date'].max().date()})")
    return len(df)


def _csi_to_df(code: str, rows: list) -> pd.DataFrame:
    """中证官网 index-perf 全字段：tradeDate/open/high/low/close/tradingVol/tradingValue。
    大盘天气的量价策略需要 OHLCV，必须保留 open/high/low/volume（旧版只留 close）。"""
    df = pd.DataFrame([{
        "date": pd.to_datetime(r["tradeDate"]),
        "open": float(r.get("open") if r.get("open") is not None else r["close"]),
        "close": float(r["close"]),
        "high": float(r.get("high") if r.get("high") is not None else r["close"]),
        "low": float(r.get("low") if r.get("low") is not None else r["close"]),
        "volume": float(r.get("tradingVol") or 0.0),
    } for r in rows])
    df.insert(0, "code", code)
    return df


def ensure_csi_ohlcv(s: requests.Session, code: str) -> int:
    """旧版 CSI parquet 只有 date/close 列（缺 OHLCV，量价策略无法计算）：
    检测到缺 open/high/low/volume 时全量重拉该代码做一次升级（幂等）。"""
    p = f"{C.INDEX_DIR}/{code}.parquet"
    old = C.read_df(p)
    need = {"open", "high", "low", "volume"}
    if len(old) and need <= set(old.columns):
        return 0
    end = datetime.date.today().strftime("%Y%m%d")
    rows = fetch_csi(s, code, "20130719", end)
    if not rows:
        return 0
    new = _csi_to_df(code, rows).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    C.write_df(new, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: 升级补全 OHLCV -> {len(new)} 行 "
          f"({new['date'].min().date()} ~ {new['date'].max().date()})")
    return len(new)


def _tx_to_df(code: str, bars: list) -> pd.DataFrame:
    """腾讯 fqkline bars: [date,open,close,high,low,volume]（无 amount，置 0）"""
    rows = []
    for k in bars:
        try:
            rows.append({"date": pd.to_datetime(k[0]), "open": float(k[1]),
                         "close": float(k[2]), "high": float(k[3]), "low": float(k[4]),
                         "volume": float(k[5]), "amount": 0.0})
        except (ValueError, TypeError, IndexError):
            continue
    df = pd.DataFrame(rows)
    df.insert(0, "code", code)
    return df


def _fill_from_tx(s: requests.Session, code: str, sym: str, old: pd.DataFrame) -> int:
    """CSI 无增量时，用腾讯补齐至 last_complete_day（仅价格指数）。"""
    p = f"{C.INDEX_DIR}/{code}.parquet"
    last = old["date"].max() if len(old) else pd.Timestamp("2013-07-19")
    cutoff = pd.Timestamp(last_complete_day())
    if len(old) and last >= cutoff:
        return len(old)
    start = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    bars = fetch_tx_kline(s, sym, start, cutoff.strftime("%Y-%m-%d"))
    new = _tx_to_df(code, bars)
    if new.empty:
        return len(old)
    new = new[(new["date"] > last) & (new["date"] <= cutoff)]
    if new.empty:
        return len(old)
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    C.write_df(df, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: TX兜底 +{len(new)} 行 -> {len(df)} 行 "
          f"({df['date'].max().date()})")
    return len(df)


def update_csi(s: requests.Session, code: str) -> int:
    p = f"{C.INDEX_DIR}/{code}.parquet"
    old = C.read_df(p)
    last = old["date"].max() if len(old) else pd.Timestamp("2013-07-19")
    today = datetime.date.today().strftime("%Y%m%d")
    start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
    if start > today:
        return len(old)
    try:
        rows = fetch_csi(s, code, start, today)
    except Exception as e:
        print(f"[fetch_index] {code}: CSI 失败 ({e})")
        rows = []
    if rows:
        new = _csi_to_df(code, rows)
        new = new[new["date"] > last]
        if not new.empty:
            df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
            df = df.sort_values("date").reset_index(drop=True)
            C.write_df(df, p, sort=["code", "date"])
            print(f"[fetch_index] {code}: +{len(new)} 行 -> {len(df)} 行 ({df['date'].max().date()})")
            return len(df)
        old = C.read_df(p)
    # CSI 空增量或失败：东财 → 腾讯（有则用）；全收益 H20269/H00300 无备用通道
    cur = old if len(old) else C.read_df(p)
    secid = CSI_EM_FALLBACK.get(code)
    if secid:
        n = _fill_from_em(s, code, secid, cur)
        cur = C.read_df(p)
        if len(cur) and cur["date"].max() >= pd.Timestamp(last_complete_day()):
            return n
    sym = CSI_TX_FALLBACK.get(code)
    if sym:
        return _fill_from_tx(s, code, sym, cur if len(cur) else C.read_df(p))
    if not secid and not sym:
        cutoff = pd.Timestamp(last_complete_day())
        last = cur["date"].max() if len(cur) else None
        if last is None or last < cutoff:
            print(f"[fetch_index] {code}: CSI 无增量且无备用通道 "
                  f"(last={None if last is None else last.date()} < {cutoff.date()})；"
                  f"依赖仓库旧数据，健康闸门将拦截过期发布")
    return len(cur) if len(cur) else len(C.read_df(p))


def update_tx(s: requests.Session, code: str, sym: str) -> int:
    """腾讯通道增量更新主要指数（10 年口径，回填用 --backfill）。"""
    p = f"{C.INDEX_DIR}/{code}.parquet"
    old = C.read_df(p)
    last = old["date"].max() if len(old) else pd.Timestamp(TX_BACKFILL_START)
    # 完整 bar 截止日（避免盘中半截）
    cutoff = pd.Timestamp(last_complete_day())
    end = datetime.date.today().isoformat()
    start = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if start > end:
        return len(old)
    bars = fetch_tx_kline(s, sym, start, end)
    new = _tx_to_df(code, bars)
    if new.empty:  # 无新增（已是最新/节假日）：合法空增量，不构造过滤
        return len(old)
    new = new[(new["date"] > last) & (new["date"] <= cutoff)]
    if new.empty:
        return len(old)
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    C.write_df(df, p, sort=["code", "date"])
    print(f"[fetch_index] {code}: +{len(new)} 行 -> {len(df)} 行 ({df['date'].max().date()})")
    return len(df)


def main() -> None:
    C.ensure_dirs()
    backfill = "--backfill" in sys.argv
    s = make_session()
    for code in CSI_CODES:
        ensure_csi_ohlcv(s, code)
        update_csi(s, code)
    for code, (sym, name) in TX_CODES.items():
        if backfill and os.path.exists(f"{C.INDEX_DIR}/{code}.parquet"):
            # 回填：删除 3 年短序列，全量重拉 10 年
            os.remove(f"{C.INDEX_DIR}/{code}.parquet")
        update_tx(s, code, sym)
    # 更新指数清单
    idx = C.read_df(f"{C.META}/indices.parquet")
    if len(idx):
        for _, row in idx.iterrows():
            p = f"{C.INDEX_DIR}/{row['code']}.parquet"
            if os.path.exists(p):
                d = pd.read_parquet(p)
                if len(d):
                    idx.loc[_, "start"] = str(d["date"].min().date())
                    idx.loc[_, "end"] = str(d["date"].max().date())
                    idx.loc[_, "n"] = len(d)
        # 补入新增指数行（大盘天气锚定：399001/399006/932000）
        existing = set(idx["code"])
        for meta in NEW_INDEX_META:
            if meta["code"] in existing:
                continue
            p = f"{C.INDEX_DIR}/{meta['code']}.parquet"
            if os.path.exists(p):
                d = pd.read_parquet(p)
                meta = dict(meta, start=str(d["date"].min().date()),
                            end=str(d["date"].max().date()), n=len(d))
            else:
                meta = dict(meta, start="", end="", n=0)
            idx = pd.concat([idx, pd.DataFrame([meta])], ignore_index=True)
            print(f"[fetch_index] 清单新增 {meta['code']} {meta['name']}")
        C.write_df(idx, f"{C.META}/indices.parquet", sort=["code"])
    C.manifest_add({"event": "fetch_index", "at": C.bj_now(), "backfill": backfill})
    print("指数更新完成")


if __name__ == "__main__":
    main()
