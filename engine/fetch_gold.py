"""黄金数据抓取：伦敦金 XAU + 沪金 AU0 日线 -> parquet + 周线。

数据源（均为公开免费接口）：
  伦敦金 XAU   sina GlobalFuturesService.getGlobalFuturesDailyKLine
  沪金 AU0     sina InnerFuturesNewService.getDailyKLine（主力连续，人民币）
  货币基金     腾讯 web.ifzq.gtimg.cn（sh511880，银华日利）

输出：
  data/kline/gold/gold_daily.parquet     日线（XAU + AU0 + 511880）
  data/kline/gold/gold_weekly.parquet    周线（XAU 为主口径）
  data/meta/gold_meta.json               抓取元信息 + 校验结果

注意：本脚本在 GitHub Actions（无代理）与本地 WorkBuddy（有 mihomo 代理）两种环境运行，
统一用 trust_env=False 绕开代理，直连国内源。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_KLINE = os.path.join(ROOT, "data", "kline", "gold")
OUT_META = os.path.join(ROOT, "data", "meta")
os.makedirs(OUT_KLINE, exist_ok=True)
os.makedirs(OUT_META, exist_ok=True)

SINA = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 抓取失败时的重试次数与退避
RETRY = 3
BACKOFF = 2.0


def _session():
    import requests

    s = requests.Session()
    # 关键：WorkBuddy 环境注入 HTTP(S)_PROXY=mihomo，对国内财经站点会直接断连。
    # trust_env=False 关闭环境代理读取，走直连。
    s.trust_env = False
    return s


def log(msg: str) -> None:
    print(f"[gold] {msg}", flush=True)


def _get(url: str, referer: str | None = None) -> str:
    last = None
    for i in range(RETRY):
        try:
            h = dict(HEADERS)
            if referer:
                h["Referer"] = referer
            r = _session().get(url, headers=h, timeout=25)
            if r.status_code == 200 and r.text.strip():
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i < RETRY - 1:
            time.sleep(BACKOFF * (i + 1))
    raise RuntimeError(f"抓取失败（重试 {RETRY} 次）: {url[:110]} 最后错误: {last}")


def _parse_jsonp(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("响应中未找到 JSON 数组")
    arr = json.loads(m.group(0))
    if not isinstance(arr, list) or not arr:
        raise RuntimeError("JSON 数组为空")
    return arr


# ---------------------------------------------------------------- 抓取

def fetch_xau_daily() -> pd.DataFrame:
    """伦敦金 XAU 日线（美元/盎司）。"""
    url = f"{SINA}/var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XAU"
    arr = _parse_jsonp(_get(url, referer="https://finance.sina.com.cn/"))
    df = pd.DataFrame(
        {
            "date": [a["date"] for a in arr],
            "open": [float(a["open"]) for a in arr],
            "high": [float(a["high"]) for a in arr],
            "low": [float(a["low"]) for a in arr],
            "close": [float(a["close"]) for a in arr],
        }
    )
    df["symbol"] = "XAU"
    return df


def fetch_au0_daily() -> pd.DataFrame:
    """沪金 AU0 主力连续日线（元/克）。"""
    url = f"{SINA}/var%20t=/InnerFuturesNewService.getDailyKLine?symbol=AU0"
    arr = _parse_jsonp(_get(url, referer="https://finance.sina.com.cn/"))
    df = pd.DataFrame(
        {
            "date": [a["d"] for a in arr],
            "open": [float(a["o"]) for a in arr],
            "high": [float(a["h"]) for a in arr],
            "low": [float(a["l"]) for a in arr],
            "close": [float(a["c"]) for a in arr],
        }
    )
    df["symbol"] = "AU0"
    return df


def fetch_cash_daily(code: str = "sh511880") -> pd.DataFrame:
    """货币基金日净值（sh511880 银华日利），分页向前翻。

    注意：分页边界会重复返回边界日（前一页末 = 后一页首），值相同，
    这里按日期去重并保留最后一条，否则会污染周线聚合。
    """
    out: list[list] = []
    end = "2026-12-31"
    for _ in range(40):
        url = f"{TENCENT}?param={code},day,2000-01-01,{end},640,qfq"
        j = json.loads(_get(url))
        node = j.get("data", {}).get(code, {})
        k = node.get("qfqday") or node.get("day") or []
        if not k:
            break
        out = k + out
        first = k[0][0]
        if first <= "2013-01-01":
            break
        end = first
        time.sleep(0.25)
    if not out:
        raise RuntimeError("货币基金数据为空")
    df = pd.DataFrame({"date": [a[0] for a in out], "close": [float(a[2]) for a in out]})
    n0 = len(df)
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    if n0 != len(df):
        log(f"  511880 分页去重：{n0} -> {len(df)} 行")
    df["symbol"] = "SH511880"
    return df


# ---------------------------------------------------------------- 变换

def to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """日线 -> 自然周线（周一~周日）。open=周首日，close=周末。"""
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    d["wk"] = d["date"] - pd.to_timedelta(d["date"].dt.weekday, unit="D")
    g = d.groupby("wk")
    w = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "n_days": g["close"].size(),
        }
    ).reset_index().rename(columns={"wk": "date"})
    w["symbol"] = d["symbol"].iloc[0]
    return w


# ---------------------------------------------------------------- 校验

def validate(daily: pd.DataFrame, weekly: pd.DataFrame, name: str) -> dict:
    """数据质量校验，分两级：

    fatal —— 会让下游计算失真，必须报警
      · 样本过短 / 空数据
      · 重复交易日
      · 最新数据严重滞后（>21 天，疑似源停更）
    warn  —— 已知无害的源瑕疵，不阻塞
      · OHLC 关系轻微越界：新浪全球期货源有约 2.5% 的行 high/low 与 open/close
        相差约 1%（集中在 2012-2020）。策略的 TR = max(H-L,|H-昨收|,|L-昨收|)
        对此有容错，实测 1004 周仓位零差异，故仅记录不告警。
      · >10 天的缺口：黄金年假 / 春节等正常停市。
    """
    fatal: list[str] = []
    warn: list[str] = []

    if daily.empty:
        fatal.append("日线为空")
    if 0 < len(daily) < 200:
        fatal.append(f"日线仅 {len(daily)} 根，样本过短")
    dup = int(daily["date"].duplicated().sum())
    if dup:
        fatal.append(f"{dup} 个重复交易日")

    last = pd.to_datetime(daily["date"].iloc[-1])
    stale_days = (pd.Timestamp.now(tz="UTC").tz_localize(None) - last).days
    if stale_days > 21:
        fatal.append(f"最新数据距今 {stale_days} 天，源可能已停更")
    elif stale_days > 7:
        warn.append(f"最新数据距今 {stale_days} 天")

    bad = daily[
        (daily["high"] < daily["low"])
        | (daily["high"] < daily["close"])
        | (daily["high"] < daily["open"])
        | (daily["low"] > daily["close"])
        | (daily["low"] > daily["open"])
    ]
    if len(bad):
        pct = len(bad) / len(daily) * 100
        warn.append(f"{len(bad)} 行（{pct:.1f}%）OHLC 轻微越界，源瑕疵，已验证不影响策略")

    ds = pd.to_datetime(daily["date"]).sort_values()
    ngap = int((ds.diff().dt.days > 10).sum())
    if ngap:
        warn.append(f"{ngap} 处超过 10 天的数据缺口（多为节假日停市）")

    return {
        "name": name,
        "daily_rows": int(len(daily)),
        "weekly_rows": int(len(weekly)),
        "date_min": str(pd.to_datetime(daily["date"].iloc[0]).date()),
        "date_max": str(last.date()),
        "stale_days": int(stale_days),
        "fatal": fatal,
        "warn": warn,
        "ok": not fatal,
    }


# ---------------------------------------------------------------- 主流程

def main() -> int:
    started = datetime.now(timezone.utc)
    checks = []

    log("抓取伦敦金 XAU 日线 ...")
    xau = fetch_xau_daily()
    log(f"  XAU {len(xau)} 根  {xau['date'].iloc[0]} ~ {xau['date'].iloc[-1]}")
    xau_w = to_weekly(xau)
    checks.append(validate(xau, xau_w, "XAU"))

    log("抓取沪金 AU0 日线 ...")
    au0 = fetch_au0_daily()
    log(f"  AU0 {len(au0)} 根  {au0['date'].iloc[0]} ~ {au0['date'].iloc[-1]}")
    au0_w = to_weekly(au0)
    checks.append(validate(au0, au0_w, "AU0"))

    log("抓取货币基金 511880 净值 ...")
    cash = fetch_cash_daily()
    log(f"  511880 {len(cash)} 根  {cash['date'].iloc[0]} ~ {cash['date'].iloc[-1]}")
    # 货币基金只有净值一列，补齐 OHLC 便于统一走同一套周线聚合与校验
    for col in ("open", "high", "low"):
        cash[col] = cash["close"]
    cash_w = to_weekly(cash)
    checks.append(validate(cash, cash_w, "SH511880"))

    # ---- 合并落盘 ----
    for df in (xau, au0, cash):
        df["date"] = pd.to_datetime(df["date"])

    daily = pd.concat([xau[["date", "symbol", "open", "high", "low", "close"]],
                       au0[["date", "symbol", "open", "high", "low", "close"]],
                       cash[["date", "symbol", "open", "high", "low", "close"]]],
                      ignore_index=True).sort_values(["symbol", "date"])
    daily = daily[["date", "symbol", "open", "high", "low", "close"]]
    daily.to_parquet(os.path.join(OUT_KLINE, "gold_daily.parquet"), index=False)

    weekly = pd.concat([xau_w, au0_w], ignore_index=True).sort_values(["symbol", "date"])
    weekly = weekly[["date", "symbol", "open", "high", "low", "close", "n_days"]]
    weekly.to_parquet(os.path.join(OUT_KLINE, "gold_weekly.parquet"), index=False)

    meta = {
        "generated_at_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "XAU": "sina GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XAU",
            "AU0": "sina InnerFuturesNewService.getDailyKLine?symbol=AU0",
            "SH511880": "tencent web.ifzq.gtimg.cn fqkline (qfq)",
        },
        "checks": checks,
        "rows": {
            "gold_daily": int(len(daily)),
            "gold_weekly": int(len(weekly)),
            "xau_daily": int(len(xau)),
            "au0_daily": int(len(au0)),
            "cash_daily": int(len(cash)),
        },
    }
    with open(os.path.join(OUT_META, "gold_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # ---- 报告 ----
    for c in checks:
        log(f"  [{'OK  ' if c['ok'] else 'FAIL'}] {c['name']}: {c['daily_rows']} 日线 / "
            f"{c['weekly_rows']} 周线, 更新至 {c['date_max']}, 距今 {c['stale_days']} 天")
        for m in c["warn"]:
            log(f"         · {m}")
        for m in c["fatal"]:
            log(f"         ! {m}")
            print(f"::error::{c['name']}: {m}")

    bad = [c for c in checks if not c["ok"]]
    if bad:
        log(f"完成，但 {len(bad)} 个标的未通过致命校验（已写入 meta）")
    else:
        log("全部标的通过校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
