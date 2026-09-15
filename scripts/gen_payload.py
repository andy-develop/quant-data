#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门户数据融合生成器：从 quant-data 单一数据源生成门户 PAYLOAD 全部数据段。

输入（全部来自 quant-data data/）：
  - kline/index/H20269|H30269.parquet      → 红利低波 snapshot+backtest（四态仓位机默认参数）
  - kline/index/H00300|000300.parquet      → 沪深300 择时 snapshot+backtest（v8.0 变体参数）
  - kline/etf/etf_kline.parquet            → 行业轮动 sector（21 行业 × 32 ETF）
  - snapshot/etf_<day>.parquet / index_<day>.parquet → morning 上午实时段（11:30 快照）
  - factors/stock.parquet + meta/stocks.parquet      → 选股 stocks + factors

复用仓库内 engine/ 统一引擎（原 red-dividend-strategy，代码单份、口径与产品完全一致）：
  - engine/backtest/engine.py：四态仓位机（默认=红利低波；make_params 变体=沪深300）
  - engine/update.py：build_snapshot / build_backtest_payload / trade_day_info / validate_data
  - engine/sector_engine.py + engine/sector_universe.py + engine/sector_update.build_sector_payload：行业轮动

输出（data/payload/，供 quant-portal/build_portal.py 消费）：
  - hl.json       {"snapshot","backtest","params"}      红利低波
  - hs300.json    {"snapshot","backtest","params"}      沪深300 择时
  - sector.json   sector 段（排行/持仓/风控/回测/ETF 映射/披露）
  - stock.json    {"stocks":[[code,name],...],"factors":{code:{close,mom_20,mom_60,vol_20}}}
  - morning.json  {"fetched_at","etf":[...],"index":[...]}  上午实时（今天 11:30 快照）

用法: python3 scripts/gen_payload.py
"""
import datetime
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

# ---- 复用仓库内 engine/ 引擎（原 red-dividend-strategy 迁入，单仓库无 sibling 依赖） ----
RED = os.path.join(C.BASE, "engine")
# 注意顺序：必须先插 backtest 再插 RED，否则 engine/ 顶层若出现 engine.py 会遮蔽 backtest/engine.py
sys.path.insert(0, RED)
sys.path.insert(0, os.path.join(RED, "backtest"))

import engine as EBT  # noqa: E402  backtest/engine.py（v7.12 参数化四态仓位机）
if "backtest" not in EBT.__file__.replace(os.sep, "/"):
    raise SystemExit(f"[gen_payload] 引擎错载 {EBT.__file__}（需 backtest/engine.py）")
from update import (build_backtest_payload, build_snapshot,  # noqa: E402
                    trade_day_info, validate_data)
import sector_universe as SU  # noqa: E402
import sector_engine as SE  # noqa: E402
import sector_update  # noqa: E402

PAY = C.PAYLOAD_DIR

# 沪深300 变体参数（v8.0 定稿，与 hs300_update.py 一致）
HS300_PARAMS = {"X_UP": 15.0, "Y_DOWN": 14.0, "HOLD_DAYS": 120}


def bj_now() -> str:
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M"))


def load_index_df(tr_code: str, px_code: str) -> pd.DataFrame:
    """从指数 parquet 构造引擎输入 df（date / close=全收益 / px=价格 / vol 占位）。
    只保留两序列都有的日期（与 engine.get_prices 的 inner merge 一致）。"""
    tr = C.read_df(f"{C.INDEX_DIR}/{tr_code}.parquet")[["date", "close"]]
    px = C.read_df(f"{C.INDEX_DIR}/{px_code}.parquet")[["date", "close"]].rename(columns={"close": "px"})
    df = tr.merge(px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    df["vol"] = float("nan")  # 量价背离（DIV_2OF3=False）不使用；保留列保持引擎兼容
    return df


def run_timing(df_all: pd.DataFrame, p=None, label: str = ""):
    """四态仓位机：信号(px) + T+1 撮合 + 净值核算 → {snapshot, backtest, params}。
    p=None 用引擎默认参数（红利低波）；p=make_params(...) 为变体（沪深300）。"""
    if p is not None:
        df_all = EBT.build_signals(df_all, p=p)
    else:
        df_all = EBT.build_signals(df_all)
        p = EBT  # build_snapshot 用模块默认参数对象
    # 估值剪刀差分位数大面积缺失时直接红掉（与 update.py 同闸门）
    if p.VAL_GATE:
        sp = df_all["spread_pct"].dropna()
        if len(sp) < len(df_all) * 0.5:
            raise RuntimeError(f"{label} 估值剪刀差分位数缺失 "
                               f"{int(len(df_all) - len(sp))}/{len(df_all)} 行（cn10y 未刷新？），拒绝发布")
    warnings = validate_data(df_all, hard=False)
    for w in warnings:
        print(f"  [警告] {label}: {w}")
    trades, closed, positions, state, pos, legs, t0 = EBT.replay(df_all, t1=True, start=EBT.START, p=p)
    df = df_all[df_all["date"] >= pd.Timestamp(EBT.START)].reset_index(drop=True)
    ec = EBT.equity_curve(df, trades, positions, p=p)
    m = EBT.metrics(ec, trades)
    os_stat = EBT.oversold_stats(closed)
    print(f"  {label}: 最新 {df['date'].iloc[-1].date()} 状态 {state} 仓位 {int(pos * 100)}% | "
          f"回测 {m['total'] * 100:+.1f}% / 夏普 {m['sharpe']:.2f} / 回撤 {m['mdd'] * 100:.1f}% / {m['n_trades']} 笔")
    bt = build_backtest_payload(df, trades, ec, m, os_stat, EBT.overview_stats(trades, closed, df))
    snap = build_snapshot(df_all, state, pos, legs, t0, trades, os_stat, warnings,
                          cal=trade_day_info(), p=p)
    return {"snapshot": snap, "backtest": bt}


def build_sector_raw() -> dict:
    """quant-data ETF 日K + 沪深300 指数 → sector_engine 需要的 raw dict。
    klines 行格式与 sector_universe.fetch_em_kline 一致（11 列 CSV），
    amplitude/pct/change 为 quant-data 未存字段，置 0（引擎只消费 close/amount/turnover）。"""
    etf = C.read_df(f"{C.ETF_DIR}/etf_kline.parquet")
    raw: dict = {"fetched_at": bj_now(), "etfs": {}, "csi": {}}
    for code, g in etf.groupby("code"):
        g = g.sort_values("date")
        first = g.iloc[0]
        lines = []
        for r in g.itertuples(index=False):
            lines.append(f"{r.date:%Y-%m-%d},{r.open},{r.close},{r.high},{r.low},"
                         f"{r.volume},{r.amount},0,0,0,{r.turnover}")
        raw["etfs"][code] = {"industry": str(first.industry), "name": str(first.name), "klines": lines}
    for c in ("H00300", "000300"):
        d = C.read_df(f"{C.INDEX_DIR}/{c}.parquet")
        raw["csi"][c] = [{"tradeDate": r.date.strftime("%Y-%m-%d"), "close": float(r.close)}
                         for r in d.itertuples(index=False)]
    return raw


def run_sector() -> dict:
    raw = build_sector_raw()
    print(f"  sector: ETF {len(raw['etfs'])} 只 / CSI {len(raw['csi']['H00300'])} 行")
    panel = SE.build_panel(raw)
    fac = SE.compute_factors(panel)
    bt = SE.backtest(panel, fac)
    snap = SE.snapshot(panel, fac, bt)
    m = snap["metrics"] or {}
    print(f"  sector: 覆盖 {len(panel['inds'])} 行业 / {panel['meta']['n_dates']} 交易日，"
          f"最新 {snap['data_date']} | 回测 {m.get('total', 0) * 100:+.1f}% / "
          f"夏普 {m.get('sharpe', 0):.2f} / 回撤 {m.get('mdd', 0) * 100:.1f}% / {m.get('n_trades', 0)} 笔")
    return sector_update.build_sector_payload(panel, fac, bt, snap, [], raw)


def build_stock_payload() -> dict:
    """选股段：最新因子日非空行 → {stocks:[[code,name],...], factors:{code:{...}}}。
    股票代码输出纯 6 位（与门户既有 STOCK_UNIVERSE 格式一致）。"""
    f = C.read_df(f"{C.FACTOR_DIR}/stock.parquet")
    if f.empty:
        return {"stocks": [], "factors": {}}
    meta = C.read_df(f"{C.META}/stocks.parquet")
    name_of = dict(zip(meta["code"], meta["name"]))
    # status 在 parquet 中为字符串 "1"/"0"，统一 astype(str) 比较，避免空集
    alive = set(meta.loc[meta["status"].astype(str) == "1", "code"]) if "status" in meta else set(name_of)
    # 展示用真实收盘价（raw），与因子口径（hfq 复权）分离；取与因子同日的 raw 收盘
    raw = C.stock_read("raw")[["code", "date", "close"]]
    raw_last = raw[raw["date"] == f["date"].max()]
    close_of = dict(zip(raw_last["code"], raw_last["close"]))
    last = f[f["date"] == f["date"].max()]
    stocks, factors = [], {}
    for r in last.itertuples(index=False):
        if pd.isna(r.ret_20) or pd.isna(r.vol20):
            continue
        if r.code not in alive:
            continue
        code6 = r.code.split(".")[1]
        name = name_of.get(r.code, code6)
        stocks.append([code6, name])
        factors[code6] = {"close": round(float(close_of.get(r.code, r.close)), 2),
                          "mom_20": round(float(r.ret_20), 4),
                          "mom_60": round(float(r.ret_60), 4) if not pd.isna(r.ret_60) else None,
                          "vol_20": round(float(r.vol20), 4)}
    print(f"  选股: {len(stocks)} 只（因子日 {last['date'].max().date()}）")
    return {"stocks": stocks, "factors": factors}


def build_morning() -> dict:
    """上午实时段：读取今天 11:30 快照（fetch_snapshot 产物）。
    快照缺失（周末/节假日/任务未跑）时输出空段，页面降级提示。
    day 用北京时区（CI 运行 UTC 04:00=北京 12:00，避免 UTC 日期跨天）。"""
    day = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
           .date().strftime("%Y%m%d"))
    etf = C.read_df(f"{C.SNAP_DIR}/etf_{day}.parquet")
    idx = C.read_df(f"{C.SNAP_DIR}/index_{day}.parquet")
    fetched_at = etf.attrs.get("fetched_at") if not etf.empty else None
    if fetched_at is None and not idx.empty:
        fetched_at = idx.attrs.get("fetched_at")
    out = {"fetched_at": fetched_at,
           "etf": _records(etf) if not etf.empty else [],
           "index": _records(idx) if not idx.empty else []}
    print(f"  morning: fetched_at={fetched_at} ETF {len(out['etf'])} / 指数 {len(out['index'])}")
    return out


def _records(df: pd.DataFrame) -> list:
    """DataFrame → JSON 安全 records（datetime 转 ISO 字符串、NaN/NaT/NA → None，
    避免前端 JSON.parse 因非法 NaN 直接崩）。StringDtype 的 .where 不生效，逐值清理。"""
    d = df.copy()
    for col in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[col]):
            d[col] = d[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for row in d.to_dict("records"):
        row = {k: (None if v is pd.NA
                   or (isinstance(v, float) and v != v)  # NaN
                   else v) for k, v in row.items()}
        out.append(row)
    return out


def _write(p: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    print(f"  -> {os.path.basename(p)} ({len(json.dumps(obj)) // 1024} KB)")


def main() -> None:
    C.ensure_dirs()
    print("[gen_payload] 红利低波（H20269/H30269，默认参数）...")
    hl = run_timing(load_index_df("H20269", "H30269"), p=None, label="红利低波")
    hl["params"] = {"x_up": EBT.X_UP, "y_down": EBT.Y_DOWN, "hold_days": EBT.HOLD_DAYS}
    _write(f"{PAY}/hl.json", hl)

    print("[gen_payload] 沪深300（H00300/000300，v8.0 变体）...")
    P = EBT.make_params(**HS300_PARAMS)
    hs300 = run_timing(load_index_df("H00300", "000300"), p=P, label="沪深300")
    hs300["params"] = {"x_up": P.X_UP, "y_down": P.Y_DOWN, "hold_days": P.HOLD_DAYS}
    _write(f"{PAY}/hs300.json", hs300)

    print("[gen_payload] 行业轮动（32 ETF）...")
    _write(f"{PAY}/sector.json", run_sector())

    print("[gen_payload] 选股（股票因子最新日）...")
    _write(f"{PAY}/stock.json", build_stock_payload())

    print("[gen_payload] 上午实时（morning）...")
    _write(f"{PAY}/morning.json", build_morning())

    C.manifest_add({"event": "gen_payload", "at": C.bj_now(),
                    "hl_last": hl["backtest"]["end"], "hs300_last": hs300["backtest"]["end"]})
    print("[gen_payload] 完成")


if __name__ == "__main__":
    main()
