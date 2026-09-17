#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取沪深300ETF（510300）期权：ATM 隐含波动率 + 持仓量 PCR。

数据源：新浪财经 hq.sinajs.cn（300ETF 期权合约列表 + 实时行情字段）。
落库：data/kline/option/hs300_option_daily.parquet（按交易日幂等追加）。

字段约定（与新浪 CON_OP 行情一致）：
  [5]=持仓量  [7]=行权价  [38]=隐含波动率(%)  [45]=C/P

ATM IV：取距标的现价最近的 Call/Put 各一档，IV 等权均值；若单边缺失则用另一边。
OI PCR：全部近月合约 Put 持仓合计 / Call 持仓合计。

用法: python3 scripts/fetch_option_hs300.py
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

UA = {
    "User-Agent": C.UA,
    "Referer": "https://finance.sina.com.cn",
}
OPT_DIR = os.path.join(C.KDIR, "option")
OUT = os.path.join(OPT_DIR, "hs300_option_daily.parquet")
UNDERLYING = "510300"


def _get(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "gbk"
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _underlying_px() -> float:
    text = _get(f"https://hq.sinajs.cn/list=sh{UNDERLYING}")
    # var hq_str_sh510300="名称,今开,昨收,最新,..."
    m = re.search(r'="([^"]*)"', text)
    if not m or not m.group(1):
        raise RuntimeError("标的行情为空")
    parts = m.group(1).split(",")
    px = float(parts[3])
    if px <= 0:
        raise RuntimeError(f"标的价异常: {px}")
    return px


def _contract_months() -> list[str]:
    """返回形如 2609 的近月代码列表（最多 3 个近月）。"""
    url = ("https://stock.finance.sina.com.cn/futures/api/openapi.php/"
           "StockOptionService.getStockName?exchange=null&cate=300ETF&date=&contract=")
    js = requests.get(url, headers={**UA, "Referer": "https://stock.finance.sina.com.cn"},
                      timeout=20).json()
    months = js.get("result", {}).get("data", {}).get("contractMonth") or []
    out = []
    for m in months:
        # "2026-09" → "2609"
        if isinstance(m, str) and len(m) >= 7 and "-" in m:
            y, mo = m.split("-")[:2]
            out.append(y[-2:] + mo)
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:3] or []


def _list_contracts(month_yyMm: str) -> tuple[list[str], list[str]]:
    up = _get(f"https://hq.sinajs.cn/list=OP_UP_{UNDERLYING}{month_yyMm}")
    dn = _get(f"https://hq.sinajs.cn/list=OP_DOWN_{UNDERLYING}{month_yyMm}")
    calls = re.findall(r"CON_OP_(\d+)", up)
    puts = re.findall(r"CON_OP_(\d+)", dn)
    return calls, puts


def _quote_many(codes: list[str]) -> list[list[str]]:
    rows = []
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        lst = ",".join(f"CON_OP_{c}" for c in chunk)
        text = _get(f"https://hq.sinajs.cn/list={lst}")
        for line in text.split(";"):
            if '="' not in line:
                continue
            body = line.split('="', 1)[1].rstrip('"').rstrip("\n")
            if not body:
                continue
            parts = body.split(",")
            if len(parts) >= 46:
                rows.append(parts)
        time.sleep(0.15)
    return rows


def _parse_row(parts: list[str]) -> dict | None:
    try:
        oi = float(parts[5])
        strike = float(parts[7])
        iv = float(parts[38])
        side = parts[45].strip().upper()
        dte = int(parts[47]) if str(parts[47]).isdigit() else -1
        if side not in ("C", "P"):
            return None
        # 深度虚值/临近到期 IV 常失真；过滤极端
        if iv <= 0 or iv > 200:
            iv = float("nan")
        return {"oi": oi, "strike": strike, "iv": iv, "side": side, "dte": dte}
    except (ValueError, IndexError):
        return None


def snapshot(trade_date: str | None = None) -> dict:
    """抓取当日 ATM IV 与 OI PCR。"""
    px = _underlying_px()
    months = _contract_months()
    if not months:
        raise RuntimeError("无近月合约")

    parsed = []
    for m in months:
        calls, puts = _list_contracts(m)
        parsed.extend(filter(None, map(_parse_row, _quote_many(calls + puts))))

    if not parsed:
        raise RuntimeError("期权行情解析为空")

    call_oi = sum(r["oi"] for r in parsed if r["side"] == "C")
    put_oi = sum(r["oi"] for r in parsed if r["side"] == "P")
    pcr = (put_oi / call_oi) if call_oi > 0 else float("nan")

    # ATM IV：优先 DTE∈[20,60]（避开到期周短期 IV 失真），行权价 ±5% 内 C/P 等权
    def nearest(side: str, min_dte: int, max_dte: int, band: float):
        cand = [
            r for r in parsed
            if r["side"] == side
            and r["strike"] > 0
            and min_dte <= r.get("dte", -1) <= max_dte
            and abs(r["strike"] / px - 1.0) <= band
            and pd.notna(r["iv"])
            and 1.0 < r["iv"] < 80.0
        ]
        if not cand:
            return None
        return min(cand, key=lambda r: abs(r["strike"] - px))

    ivs = []
    for band in (0.05, 0.10):
        for min_dte, max_dte in ((20, 60), (10, 120), (1, 250)):
            c_atm = nearest("C", min_dte, max_dte, band)
            p_atm = nearest("P", min_dte, max_dte, band)
            if c_atm is not None:
                ivs.append(c_atm["iv"])
            if p_atm is not None:
                ivs.append(p_atm["iv"])
            if ivs:
                break
        if ivs:
            break
    atm_iv = float(sum(ivs) / len(ivs)) if ivs else float("nan")

    day = trade_date or dt.date.today().isoformat()
    return {
        "date": day,
        "underlying": UNDERLYING,
        "spot": round(px, 4),
        "atm_iv": None if pd.isna(atm_iv) else round(atm_iv, 4),
        "oi_pcr": None if pd.isna(pcr) else round(pcr, 6),
        "call_oi": int(call_oi),
        "put_oi": int(put_oi),
        "n_contracts": len(parsed),
        "months": ",".join(months),
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_row(row: dict) -> pd.DataFrame:
    C.ensure_dirs()
    os.makedirs(OPT_DIR, exist_ok=True)
    df_new = pd.DataFrame([row])
    df_new["date"] = pd.to_datetime(df_new["date"])
    if os.path.exists(OUT):
        old = pd.read_parquet(OUT)
        old["date"] = pd.to_datetime(old["date"])
        df = (pd.concat([old, df_new], ignore_index=True)
                .drop_duplicates(["date"], keep="last")
                .sort_values("date")
                .reset_index(drop=True))
    else:
        df = df_new
    C.write_df(df, OUT, sort=["date"])
    return df


def main():
    # 交易日对齐：用指数最新完整日；盘中则仍写今天，build 侧按 date merge
    idx_path = os.path.join(C.INDEX_DIR, "000300.parquet")
    trade_date = None
    if os.path.exists(idx_path):
        idx = pd.read_parquet(idx_path, columns=["date"])
        last = pd.to_datetime(idx["date"]).max().date()
        today = dt.date.today()
        # 收盘后（>=15:05）或休市日：对齐指数最后一日；盘中写今天便于盘后覆盖
        now = dt.datetime.now()
        if today.weekday() >= 5 or now.hour >= 15:
            trade_date = last.isoformat()
        else:
            trade_date = today.isoformat()

    row = snapshot(trade_date)
    df = save_row(row)
    print(f"option hs300: date={row['date']} atm_iv={row['atm_iv']} "
          f"oi_pcr={row['oi_pcr']} contracts={row['n_contracts']} "
          f"rows={len(df)} → {OUT}")


if __name__ == "__main__":
    main()
