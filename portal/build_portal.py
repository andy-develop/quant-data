#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门户构建脚本：从 quant-data 单一数据源生成统一门户 index.html。

数据来源（全部来自 quant-data，由 gen_payload.py 生成 data/payload/）：
  - payload/hl.json       → PAYLOAD 的 snapshot / backtest（红利低波）
  - payload/hs300.json    → PAYLOAD 的 hs300（沪深300 择时）
  - payload/sector.json   → PAYLOAD 的 sector（行业轮动）
  - payload/stock.json    → STOCK_UNIVERSE（[code,name]）+ REAL_FACTORS（因子）
  - payload/morning.json  → MORNING（上午实时快照，11:30）
  - payload/weather.json  → 大盘天气四层择时（build_weather.py 生成）
量化实验室报告（动量/黑盒）→ 优先 QLAB_REPORT 指向的 quant-lab/report/index.html
  （本地产物）；CI 上无 quant-lab，回退 data/qlab/momentum.json + blackbox.json
  （一次性抽取的实验室 payload，随数据仓库入库，懒更新）。

用法: python3 portal/build_portal.py [QLAB_REPORT=path/to/quant-lab/report/index.html]
输出: quant-data/portal/index.html（.gitignore 忽略，发布时上传）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, "template.html")
WEATHER_TAB = os.path.join(ROOT, "weather_tab.html")
OUTPUT = os.path.join(ROOT, "index.html")
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


def main():
    # 1) ETF 三段策略 PAYLOAD（quant-data 单一数据源）
    hl = load_json(os.path.join(QD, "hl.json"), {})
    hs300 = load_json(os.path.join(QD, "hs300.json"), {})
    sector = load_json(os.path.join(QD, "sector.json"), {})
    payload = dict(hl)
    if hs300:
        payload["hs300"] = hs300
    if sector:
        payload["sector"] = sector
    print(f"PAYLOAD 段: {sorted(payload.keys())}（hl 缺失={not hl}）")
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

    # 5) 量化实验室报告（动量/黑盒）
    qlab_payload = load_qlab_payload()

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

    # 7) 读模板并替换占位符
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    html = html.replace("<!--__WEATHER_VIEW__-->", weather_tab)
    html = html.replace(
        '<script id="PAYLOAD" type="application/json">__PAYLOAD__</script>',
        '<script id="PAYLOAD" type="application/json">' + js_array_str(payload) + '</script>')
    html = html.replace("/*__REAL_FACTORS__*/{}", js_array_str(factors))
    html = html.replace("/*__STOCK_UNIVERSE__*/[]", js_array_str(stocks))
    html = html.replace("/*__MORNING__*/{}", js_array_str(morning))
    html = html.replace("/*__GEN_TIME__*/", gen_time)
    html = html.replace("/*__DATA_DATE__*/", data_date)
    html = html.replace("/*__QLAB_MOMENTUM__*/{}",
                        js_array_str(qlab_payload.get("momentum", {})))
    html = html.replace("/*__QLAB_BLACKBOX__*/{}",
                        js_array_str(qlab_payload.get("blackbox", {})))

    # 8) 校验无残留占位符
    remain = [p for p in ["__PAYLOAD__", "__REAL_FACTORS__", "__STOCK_UNIVERSE__",
                          "__MORNING__", "__GEN_TIME__", "__DATA_DATE__",
                          "__QLAB_MOMENTUM__", "__QLAB_BLACKBOX__", "__WEATHER__",
                          "__WEATHER_VIEW__"] if p in html]
    if remain:
        print(f"错误: 仍有占位符未替换: {remain}")
        sys.exit(1)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUTPUT) / 1024
    print(f"生成完成: {OUTPUT} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
