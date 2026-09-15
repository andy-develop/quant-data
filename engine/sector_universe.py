#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 行业轮动 · 标的池与行情抓取（v1.1）

数据源（纯 HTTP，无 akshare，GitHub Actions 可用）：
  1) 行业 ETF 日行情：东方财富 push2his K 线接口（前复权收盘 / 成交量 / 成交额 / 换手率）
     - 前复权保证 ETF 分红除息后价格连续，信号与回测口径一致
     - 换手率 / 成交额用于拥挤度因子（换手率分位 + 成交额占比变化）
  2) 沪深300（全收益 H00300 + 价格 000300）：中证指数官网 index-perf 接口（与红利低波流水线同源）

标的池（v1.0 定稿，21 行业 × 32 只 ETF，2026-09 可用性实测）：
  按方案第二节：行业映射（每只 ETF 映射唯一主行业）→ 流动性筛选（取两市最活跃品种）
  → 同行业取 1-2 只（规模/流动性兼顾）。信号在“行业篮子”上计算（成员等权），交易映射回 ETF。
  披露（v1.1，P1-13）：标的池为 2026-09 时点人工挑选，以今日视角回溯历史存在幸存者/前视偏差；
  早期年份可选行业少（2018 年仅 4 行业入池，轮动近乎全部持有）。
"""
import json, os, time, datetime, threading
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ============ 标的池（行业 -> 成员 ETF） ============
# code: 场内代码；secid 前缀按交易所自动判断（5/6 开头=沪 1.，其余=深 0.）
UNIVERSE = [
    {"industry": "半导体",    "etfs": [("512760", "芯片ETF国泰"), ("159995", "芯片ETF华夏")]},
    {"industry": "新能源车",  "etfs": [("515030", "新能源车ETF华夏"), ("516160", "新能源ETF南方")]},
    {"industry": "光伏",      "etfs": [("515790", "光伏ETF华泰柏瑞"), ("159857", "光伏ETF天弘")]},
    {"industry": "军工",      "etfs": [("512660", "军工ETF国泰"), ("512710", "军工龙头ETF富国")]},
    {"industry": "医药",      "etfs": [("512010", "医药ETF易方达"), ("159929", "医药ETF汇添富")]},
    {"industry": "食品饮料",  "etfs": [("515170", "食品饮料ETF华夏"), ("512690", "酒ETF鹏华")]},
    {"industry": "银行",      "etfs": [("512800", "银行ETF华宝"), ("515020", "银行ETF华夏")]},
    {"industry": "非银金融",  "etfs": [("512070", "证券保险ETF易方达"), ("512000", "券商ETF华宝")]},
    {"industry": "有色金属",  "etfs": [("512400", "有色金属ETF南方")]},
    {"industry": "煤炭",      "etfs": [("515220", "煤炭ETF国泰")]},
    {"industry": "钢铁",      "etfs": [("515210", "钢铁ETF国泰")]},
    {"industry": "房地产",    "etfs": [("512200", "房地产ETF南方"), ("159768", "房地产ETF银华")]},
    {"industry": "基建",      "etfs": [("516950", "基建ETF银华")]},
    {"industry": "电力公用",  "etfs": [("159611", "电力ETF广发"), ("561560", "电力ETF华泰柏瑞")]},
    {"industry": "通信",      "etfs": [("515880", "通信ETF国泰")]},
    {"industry": "计算机",    "etfs": [("512720", "计算机ETF国泰"), ("515230", "软件ETF国泰")]},
    {"industry": "传媒",      "etfs": [("512980", "传媒ETF广发")]},
    {"industry": "农业",      "etfs": [("159825", "农业ETF富国")]},
    {"industry": "家电",      "etfs": [("159996", "家电ETF国泰")]},
    {"industry": "汽车",      "etfs": [("516110", "汽车ETF国泰")]},
    {"industry": "消费",      "etfs": [("159928", "消费ETF汇添富")]},
]

ALL_ETFS = [(ind["industry"], code, name) for ind in UNIVERSE for code, name in ind["etfs"]]
INDUSTRY_LIST = [ind["industry"] for ind in UNIVERSE]

# 回测起点（方案：至少覆盖 2018 年至今；行业在其成员 ETF 上市且预热充分后动态入池）
BACKTEST_START = "2018-01-01"
# 抓取左边界（覆盖 120 日动量 / R² / 拥挤度 3 年分位等全部 warm-up）
FETCH_START = "20150101"
MIN_HISTORY = 250          # 行业入池所需最少交易日（120 日动量 + 60 日 R² + 拥挤度预热）
MIN_VALID_RATIO = 0.8      # 抓取后有效行业占比低于 80% 直接判失败（宁可 job 红，不用残缺数据发布）


def secid(code):
    return ("1." if code.startswith(("5", "6")) else "0.") + code


# 东财全局限流器：并发下同一进程内请求最小间隔（实测短时间并发轰炸触发
# "Remote end closed connection"，节流后稳定）
_rate_lock = threading.Lock()
_last_req = 0.0
MIN_REQ_GAP = 0.5    # 秒/请求（≈2 req/s，东财限流窗口为全局，保守节流）


def _throttle():
    global _last_req
    with _rate_lock:
        now = time.time()
        wait = MIN_REQ_GAP - (now - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()


def fetch_em_kline(code, start=FETCH_START, end=None, retries=6, timeout=40):
    """东财日 K 线（klt=101 日线，fqt=1 前复权）。
    返回 klines 列表，每行 CSV：date,open,close,high,low,volume,amount,amplitude,pct,change,turnover。
    timeout 由调用方控制：CI 快速失败模式（SECTOR_QUICK）用 20s 防挂起。"""
    end = end or datetime.date.today().strftime("%Y%m%d")
    if start > end:
        return {"klines": []}   # 增量区间为空（基线已是最新）
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&beg={start}&end={end}")
    last = None
    for k in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
            d = j.get("data") or {}
            # v1.1（P2-5）：空响应兜底——增量区间无交易日/数据缺失也返回 {"klines": []}，
            # 避免调用方 f.result()["klines"] 抛 KeyError 被记成普通失败
            if d.get("klines") or start != FETCH_START:
                d.setdefault("klines", [])
                return d
            last = ValueError("empty klines")
        except Exception as e:
            last = e
        time.sleep(2.0 * (k + 1))
    raise RuntimeError(f"fetch EM kline {code} failed: {last}")


def fetch_csi_index(code, start="20150101", end=None, retries=4):
    """中证指数官网 index-perf（000300 沪深300 价格 / H00300 沪深300 全收益）。"""
    end = end or datetime.date.today().strftime("%Y%m%d")
    if start > end:
        return []   # 增量区间为空
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?"
           f"indexCode={code}&startDate={start}&endDate={end}")
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://www.csindex.com.cn/"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            rows = j.get("data") or []
            if rows or start != "20150101":
                return rows   # 增量区间无交易日也合法返回空
            last = ValueError("empty rows")
        except Exception as e:
            last = e
        time.sleep(2.0 + 2.0 * k)
    raise RuntimeError(f"fetch CSI {code} failed: {last}")


def fetch_universe():
    """抓取全部标的 ETF 行情 + 沪深300（全收益/价格），返回原始响应字典（供存档）。"""
    raw = {"fetched_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
           "etfs": {}, "csi": {}}
    for ind, code, name in ALL_ETFS:
        d = fetch_em_kline(code)
        raw["etfs"][code] = {"industry": ind, "name": name, "klines": d["klines"]}
    for c in ("H00300", "000300"):
        raw["csi"][c] = fetch_csi_index(c)
    return raw


if __name__ == "__main__":
    import sys
    raw = fetch_universe()
    print(f"已抓取 {len(raw['etfs'])} 只 ETF + 沪深300 全收益/价格")
    for code, v in raw["etfs"].items():
        kl = v["klines"]
        print(f"  {code} {v['name']:<12} {v['industry']:<6} {kl[0].split(',')[0]} ~ {kl[-1].split(',')[0]} ({len(kl)} 条)")
