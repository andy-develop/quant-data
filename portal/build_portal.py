#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门户构建脚本：从 quant-data 单一数据源生成轻量导航页 + 策略应用页。

数据来源（全部来自 quant-data，由 gen_payload.py 生成 data/payload/）：
  - payload/hl.json       → PAYLOAD 的 snapshot / backtest（红利低波）
  - payload/hs300.json    → PAYLOAD 的 hs300（沪深300 择时）
  - payload/hs300_score.json → PAYLOAD 的 hs300_score（沪深300 五维综合打分）
  - payload/sector.json   → PAYLOAD 的 sector（行业轮动）
  - payload/stock.json    → STOCK_UNIVERSE（[code,name]）+ REAL_FACTORS（因子）
  - payload/morning.json  → MORNING（上午实时快照，11:30）
  - payload/weather.json  → 大盘天气四层择时（build_weather.py 生成）
  - payload/timing_db.json → 择时回测 K 线包（build_timing_db.py，注入 bt_tab）
量化实验室报告（动量/黑盒）→ 优先 QLAB_REPORT 指向的 quant-lab/report/index.html
  （本地产物）；CI 上无 quant-lab，回退 data/qlab/momentum.json + blackbox.json
  （一次性抽取的实验室 payload，随数据仓库入库，懒更新）。

用法: python3 portal/build_portal.py [QLAB_REPORT=path/to/quant-lab/report/index.html]
输出:
  - portal/index.html  轻量四大板块导航（首屏入口，.gitignore）
  - portal/app.html    策略应用页（原整站内容，.gitignore）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, "template.html")
HOME_TEMPLATE = os.path.join(ROOT, "home.html")
WEATHER_TAB = os.path.join(ROOT, "weather_tab.html")
BT_TAB = os.path.join(ROOT, "bt_tab.html")
OUTPUT_APP = os.path.join(ROOT, "app.html")
OUTPUT_HOME = os.path.join(ROOT, "index.html")
BASE = os.path.dirname(ROOT)                     # quant-data 仓库根
QD = os.path.join(BASE, "data", "payload")       # gen_payload.py 产物
QLAB_DIR = os.path.join(BASE, "data", "qlab")    # 实验室 payload 归档（CI 回退）

# 量化实验室报告候选路径（按优先级探测，可用 QLAB_REPORT 环境变量覆盖）
QLAB_CANDIDATES = [
    "/Users/andy/WorkBuddy/2026-09-03-14-42-54/quant-lab/report/index.html",
    "/Users/andy/WorkBuddy/2026-09-08-10-18-26/quant-lab/report/index.html",
]


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def js_array_str(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def find_qlab_report():
    env = os.environ.get("QLAB_REPORT")
    if env:
        if os.path.exists(env):
            return env
        print(f"警告: QLAB_REPORT 指向的文件不存在: {env}，回退到默认探测")
    for p in QLAB_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def extract_qlab_payload(path):
    """从 quant-lab 报告提取 MODES / MODES_BB 两段 JSON。

    报告内 `const MODES = {...};` 与 `const MODES_BB = {...};` 各占一行超长行，
    按行前缀提取并 json.loads 校验，比跨行正则更可靠。
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    out = {}
    for line in lines:
        for key, prefix in (("momentum", "const MODES = "),
                            ("blackbox", "const MODES_BB = ")):
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                if body.endswith(";"):
                    body = body[:-1]
                out[key] = json.loads(body)
    if len(out) != 2:
        raise RuntimeError(f"quant-lab 报告中未找到 MODES/MODES_BB 两段 JSON: {list(out)}")
    print(f"量化实验室 payload: 动量 {len(out['momentum'].keys())} 个区间, "
          f"黑盒 {len(out['blackbox'].keys())} 个区间")
    return out


def load_qlab_payload():
    """实验室 payload：QLAB_REPORT 指向的本地报告 > 仓库内 data/qlab/ 归档。
    CI 无 quant-lab 报告时用归档（一次性抽取，懒更新），页面不降级。"""
    qlab_path = find_qlab_report()
    if qlab_path:
        try:
            payload = extract_qlab_payload(qlab_path)
            print(f"量化实验室 payload: 来自 {qlab_path}")
            return payload
        except Exception as e:
            print(f"警告: 提取量化实验室 payload 失败: {e}")
    # 回退 data/qlab/ 归档
    out = {}
    for key, name in (("momentum", "momentum.json"), ("blackbox", "blackbox.json")):
        p = os.path.join(QLAB_DIR, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out[key] = json.load(f)
    print(f"量化实验室 payload: 回退 data/qlab/ 归档（momentum {len(out.get('momentum', {}))} 个, "
          f"blackbox {len(out.get('blackbox', {}))} 个）")
    if len(out) != 2:
        print("警告: 动量/黑盒归档缺失，对应页面将显示数据缺失提示")
    return out


def _round_list(xs, nd=2):
    return [round(float(x), nd) for x in (xs or [])]


def _slim_trades(trades):
    out = []
    for t in trades or []:
        out.append({
            "code": t["code"],
            "name": t["name"],
            "strategy_cn": t.get("strategy_cn", ""),
            "entry_date": t["entry_date"],
            "exit_date": t["exit_date"],
            "entry_px": round(float(t["entry_px"]), 2),
            "exit_px": round(float(t["exit_px"]), 2),
            "shares": t["shares"],
            "pnl_pct": round(float(t["pnl_pct"]), 6),
            "pnl_cny": round(float(t["pnl_cny"]), 2),
            "reason": t["reason"],
            "hold_days": t["hold_days"],
            "buy_rank": t.get("buy_rank", 0),
        })
    return out


def _slim_mode(mode):
    """去掉与 shared 重复的序列，压缩 trades/holdings 浮点。"""
    if not mode:
        return mode
    m = dict(mode)
    for k in ("dates", "bench", "bench1000"):
        m.pop(k, None)
    if "equity" in m:
        m["equity"] = _round_list(m["equity"], 2)
    m["trades"] = _slim_trades(m.get("trades"))
    holds = []
    for h in m.get("holdings") or []:
        hh = dict(h)
        if "entry_px" in hh:
            hh["entry_px"] = round(float(hh["entry_px"]), 2)
        if "pnl_pct" in hh:
            hh["pnl_pct"] = round(float(hh["pnl_pct"]), 6)
        if "value" in hh:
            hh["value"] = round(float(hh["value"]), 2)
        holds.append(hh)
    m["holdings"] = holds
    return m


def compact_qlab_modes(modes):
    """压缩实验室 payload：on/off 共享 dates/bench，并圆整浮点。

    前端 hydrateQlab() 会把 shared 写回 on/off，渲染逻辑无需改字段名。
    """
    if not modes:
        return {}
    out = {}
    for win, pair in modes.items():
        if not isinstance(pair, dict):
            out[win] = pair
            continue
        on = pair.get("on") or {}
        off = pair.get("off") or {}
        shared = {
            "dates": on.get("dates") or off.get("dates") or [],
            "bench": _round_list(on.get("bench") or off.get("bench")),
            "bench1000": _round_list(on.get("bench1000") or off.get("bench1000")),
        }
        out[win] = {
            "shared": shared,
            "on": _slim_mode(on),
            "off": _slim_mode(off),
        }
    return out


def write_qlab_assets(qlab_payload):
    """把动量/黑盒 payload 写成独立 JSON，供前端按需 fetch（不再内联进 index.html）。"""
    asset_dir = os.path.join(ROOT, "qlab")
    os.makedirs(asset_dir, exist_ok=True)
    written = {}
    for key, fname in (("momentum", "momentum.json"), ("blackbox", "blackbox.json")):
        raw = qlab_payload.get(key) or {}
        compact = compact_qlab_modes(raw)
        path = os.path.join(asset_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(compact, f, ensure_ascii=False, separators=(",", ":"))
        written[key] = os.path.getsize(path)
        print(f"qlab 外置: {path} ({written[key]/1024:.1f} KB, "
              f"原始约 {len(js_array_str(raw))/1024:.1f} KB)")
    return written


def main():
    # 1) ETF 三段策略 PAYLOAD（quant-data 单一数据源）
    hl = load_json(os.path.join(QD, "hl.json"), {})
    hs300 = load_json(os.path.join(QD, "hs300.json"), {})
    hs300_score = load_json(os.path.join(QD, "hs300_score.json"), {})
    sector = load_json(os.path.join(QD, "sector.json"), {})
    payload = dict(hl)
    if hs300:
        payload["hs300"] = hs300
    if hs300_score:
        payload["hs300_score"] = hs300_score
    if sector:
        payload["sector"] = sector
    print(f"PAYLOAD 段: {sorted(payload.keys())}（hl 缺失={not hl}）")
    if hs300_score:
        snap = hs300_score.get("snapshot") or {}
        print(f"hs300_score: data_date={hs300_score.get('data_date')} "
              f"score={snap.get('score')} stance={snap.get('stance_zh')}")
    if not hl:
        print("错误: 缺少 hl.json，无法生成门户（请先运行 quant-data/scripts/gen_payload.py）")

    # 2) 选股数据（stocks + factors）
    stock = load_json(os.path.join(QD, "stock.json"), {})
    stocks = stock.get("stocks", [])
    factors = stock.get("factors", {})
    print(f"股票: {len(stocks)} 只, 因子: {len(factors)} 个")

    # 3) 上午实时快照（morning）
    morning = load_json(os.path.join(QD, "morning.json"), {})
    print(f"morning: fetched_at={morning.get('fetched_at')} "
          f"ETF {len(morning.get('etf', []))} / 指数 {len(morning.get('index', []))}")

    # 4) 时间（以 payload 内数据为准，非本地时钟）
    snap = hl.get("snapshot", {})
    gen_time = snap.get("generated_at", "—")
    data_date = snap.get("data_date", "—")

    # 5) 量化实验室报告（动量/黑盒）——外置为独立 JSON，避免撑爆 index.html
    qlab_payload = load_qlab_payload()
    write_qlab_assets(qlab_payload)

    # 6) 大盘天气四层择时：weather.json 数据 + weather_tab.html 片段
    weather = load_json(os.path.join(QD, "weather.json"), {})
    print(f"weather: data_date={weather.get('data_date')} 指数 {len(weather.get('indices', {}))} 个")
    weather_tab = ""
    if os.path.exists(WEATHER_TAB):
        with open(WEATHER_TAB, encoding="utf-8") as f:
            weather_tab = f.read()
        weather_tab = weather_tab.replace("/*__WEATHER__*/{}", js_array_str(weather))
    else:
        print("警告: 缺少 portal/weather_tab.html，天气视图降级为缺失提示")
        weather_tab = ('<div class="wzone w-missing">天气模块未打包（缺少 portal/weather_tab.html）。'
                       '</div>')

    # 6b) 择时回测：timing_db.json + bt_tab.html（index.html 移植）
    timing_db = load_json(os.path.join(QD, "timing_db.json"), {})
    if not timing_db.get("indices"):
        # 缺包时现场组装（不阻断门户；补取数逻辑在 build_timing_db）
        try:
            sys.path.insert(0, os.path.join(BASE, "scripts"))
            import build_timing_db as BTD  # noqa: WPS433
            timing_db = BTD.build()
            print("timing_db: 现场组装完成")
        except Exception as e:
            print(f"警告: timing_db 组装失败: {e}")
            timing_db = timing_db or {}
    print(f"timing_db: data_date={timing_db.get('data_date')} "
          f"指数 {len(timing_db.get('indices', {}))} / "
          f"股票 {len(timing_db.get('stocks', {}))}")
    bt_tab = ""
    if os.path.exists(BT_TAB):
        with open(BT_TAB, encoding="utf-8") as f:
            bt_tab = f.read()
        bt_tab = bt_tab.replace("/*__TIMING_DB__*/{}", js_array_str(timing_db))
    else:
        print("警告: 缺少 portal/bt_tab.html，择时回测视图降级")
        bt_tab = ('<div class="btzone" style="padding:24px;color:#92400E">'
                  '择时回测模块未打包（缺少 portal/bt_tab.html）。</div>')

    # 7) 读模板并替换占位符
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    html = html.replace("<!--__WEATHER_VIEW__-->", weather_tab)
    html = html.replace("<!--__BT_VIEW__-->", bt_tab)
    html = html.replace(
        '<script id="PAYLOAD" type="application/json">__PAYLOAD__</script>',
        '<script id="PAYLOAD" type="application/json">' + js_array_str(payload) + '</script>')
    html = html.replace("/*__REAL_FACTORS__*/{}", js_array_str(factors))
    html = html.replace("/*__STOCK_UNIVERSE__*/[]", js_array_str(stocks))
    html = html.replace("/*__MORNING__*/{}", js_array_str(morning))
    html = html.replace("/*__GEN_TIME__*/", gen_time)
    html = html.replace("/*__DATA_DATE__*/", data_date)
    # QLab 数据改为 qlab/*.json 按需加载；占位符置 null，不再内联数百 KB JSON
    html = html.replace("/*__QLAB_MOMENTUM__*/{}", "null")
    html = html.replace("/*__QLAB_BLACKBOX__*/{}", "null")

    # 8) 校验无残留占位符
    remain = [p for p in ["__PAYLOAD__", "__REAL_FACTORS__", "__STOCK_UNIVERSE__",
                          "__MORNING__", "__GEN_TIME__", "__DATA_DATE__",
                          "__QLAB_MOMENTUM__", "__QLAB_BLACKBOX__", "__WEATHER__",
                          "__WEATHER_VIEW__", "__TIMING_DB__", "__BT_VIEW__"] if p in html]
    if remain:
        print(f"错误: 仍有占位符未替换: {remain}")
        sys.exit(1)

    with open(OUTPUT_APP, "w", encoding="utf-8") as f:
        f.write(html)

    # 轻量导航页：首屏入口，不内联任何策略 payload
    if not os.path.exists(HOME_TEMPLATE):
        print(f"错误: 缺少导航模板 {HOME_TEMPLATE}")
        sys.exit(1)
    with open(HOME_TEMPLATE, encoding="utf-8") as f:
        home = f.read()
    home = home.replace("/*__DATA_DATE__*/", data_date)
    if "__DATA_DATE__" in home:
        print("错误: 导航页仍有占位符未替换: __DATA_DATE__")
        sys.exit(1)
    with open(OUTPUT_HOME, "w", encoding="utf-8") as f:
        f.write(home)

    app_kb = os.path.getsize(OUTPUT_APP) / 1024
    home_kb = os.path.getsize(OUTPUT_HOME) / 1024
    print(f"生成完成: {OUTPUT_HOME} ({home_kb:.1f} KB 导航) + {OUTPUT_APP} ({app_kb:.1f} KB 应用)")


if __name__ == "__main__":
    main()
