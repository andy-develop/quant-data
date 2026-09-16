# HANDOFF — quant-data 统一数据仓库与量化门户

> 更新：2026-09-16（数据可靠性：CSI/腾讯/东财多通道兜底、cn10y 路径修复、健康闸门加固）
> 历史演进（2026-09-15 前）详见已归档仓库 `andy-develop/red-dividend-strategy` 的 HANDOFF.md（§1-§34），本文件为唯一交接主文档。

## 1. 项目概述

一个 GitHub 私有仓库（`andy-develop/quant-data`）承载全部代码与数据，服务一个**三合一量化门户**（当前 URL 以 `data/hsk-resource.json` 为准，HSK 更新禁用会导致 URL 漂移）：

- **ETF 策略**（原 red-dividend-strategy）：红利低波（四态仓位机 v7.12+）、沪深300 择时（v8.0 变体）、行业轮动（v1.1，21 行业 × 32 ETF）
- **短线策略 / 个性化选股**（原 stock-factor-engine）：股票动量/波动因子（DuckDB 因子层）→ 选股列表
- **量化实验室**（外部 quant-lab 报告）：动量 + 黑盒，优先本地报告，回退 `data/qlab/` 归档
- **大盘天气**（四层择时，2026-09-15 新增）：5 大宽基指数天气总览 + 四层信号明细（均线状态机/量价网格/粘合清仓/融合决策），数据由 `scripts/build_weather.py` 预计算为 `data/payload/weather.json`，前端 `portal/weather_tab.html` 片段注入

数据口径：A股日K 15:00 收盘后才完整 → 工作日 12:00 门户任务信号基于 **T-1 完整收盘**；"当天上午结果" = 11:30 实时快照（morning 段双轨展示）。

## 2. 目录结构

```
quant-data/
├── scripts/            # 数据管道（fetch/build/gen/housekeeping，均幂等增量）
│   ├── common.py       # 路径/IO/manifest 基础（BASE=仓库根）
│   ├── fetch_index.py  # 指数日K：CSI(index-perf) + 腾讯 fqkline 双通道
│   ├── fetch_etf.py    # ETF 日K：东财主通道 + 腾讯兜底（PREFER_TX=1 直连腾讯）
│   ├── fetch_stock.py  # 股票日K：raw 批量快照 + hfq 逐股（腾讯双通道防 WAF）
│   ├── fetch_snapshot.py  # 11:30 上午实时快照（ETF 32 + 指数 5）
│   ├── build_factors.py   # DuckDB 股票日度因子（gitignored 确定性中间层）
│   ├── build_weather.py   # 大盘天气四层择时预计算 → payload/weather.json
│   ├── gen_payload.py     # 复用 engine/ 生成 hl/hs300/sector/stock/morning 5 段
│   └── housekeeping.py    # 滚动保留（3y/13y/7天）+ compact + 体积报告
├── engine/             # ★ 统一回测引擎（原 red-dividend-strategy 迁入）
│   ├── backtest/engine.py   # 四态仓位机（默认=红利低波，make_params 变体=沪深300）
│   ├── update.py / hs300_update.py / sector_{engine,universe,update}.py
│   ├── payload_util.py / refresh_cn10y.py / *_sensitivity.py（过拟合体检，离线工具）
│   ├── trade_calendar.csv / backtest/{cn10y,h20269,h30269}_daily.csv / sensitivity.json
│   └── tests/              # 64 项引擎单测（unittest）
├── portal/
│   ├── template.html       # 门户宿主模板（约 2400 行）
│   ├── weather_tab.html    # 大盘天气视图片段（.wzone，由 build_portal.py 注入）
│   ├── build_portal.py     # payload → index.html（qlab 优先本地报告，回退归档）
│   └── echarts.min.js      # ECharts 5.5.0 本地副本（禁 CDN，见 §5 踩坑）
├── data/               # 全部数据（增量文件入 git，见 §3 保留策略）
└── .github/workflows/ portal.yml(12:00) + mirror.yml(16:35)
```

## 3. 数据范围与保留策略（housekeeping.py 强制）

| 数据 | 范围 | 存储 | 保留 |
|---|---|---|---|
| 股票 K 线 raw/hfq | 全 A 股（腾讯式代码 `1.`/`0.`） | `kline/stock/{raw,hfq}_YYYY.parquet` 按年分片 + `{raw,hfq}_incr_YYYYMMDD.parquet` 日增量 | 3 年滚动 |
| 指数 K 线 | CSI 4（H20269/H30269/H00300/000300，13 年）+ TX 3（000001/000905/000852，10 年） | `kline/index/*.parquet` | 13 年全量（引擎预热需 div_proxy shift(252)） |
| ETF K 线 | 32 只（行业轮动池） | `kline/etf/etf_kline.parquet` | 全量 |
| 上午快照 | 32 ETF + 5 指数 11:30 实时 | `snapshot/{etf,index}_<day>.parquet` | 7 天 |
| 因子层 | 日度因子（价格/量比） | `factors/` | gitignored（每次重算） |
| payload | hl/hs300/sector/stock/morning/weather | `payload/*.json` | 入库（门户输入） |
| qlab 归档 | 动量/黑盒报告 JSON | `qlab/` | 入库 |

当前数据规模（2026-09-15）：股票 hfq **3,610,087 行 / 5,227 只**（4,776 只完整 3 年）；指数均 10.7~13.0 年；ETF 2015→最新。`.git` ≈ 302M。

## 4. 双 GHA 任务（满足"12:00 更新、14:00 前出结果"）

1. **portal.yml**（`0 4 * * 1-5` UTC = 北京 12:00，75min timeout）：ensure_calendar → **refresh_cn10y（软）** → fetch_index → fetch_etf(PREFER_TX=1) → fetch_stock（`|| warn` 非阻断）→ fetch_snapshot（不阻断）→ build_factors → gen_payload → build_weather → build_timing_db → **check_health** → build_portal → commit（含 cn10y/日历）→ **HSK 发布** → verify 线上 data_date。
2. **mirror.yml**（`35 8 * * 1-5` UTC = 北京 16:35，120min）：ensure_calendar → **refresh_cn10y（软）** → fetch_index → fetch_etf → fetch_stock → housekeeping → build_weather/timing_db → **check_health --stock-coverage** → commit（含 cn10y/日历）。

## 5. 关键实现与踩坑记录

- **股票 fqkline WAF 经验**：`ifzq/web.ifzq` 双主机均曾被封 → **首选 `proxy.finance.qq.com`**（官方代理，实测 30+ 连发不触发）；主机池故障转移（proxy → ifzq → web.ifzq），被封主机 10 分钟回归。**FQ_MAX=800**（腾讯 >800 会截断到 640 根，不足 3 年）。降级探测 `backoff=False`，防 empty 股票误触发全局退避。指数/ETF 腾讯通道已复用同一主机池（`common.tx_fqkline_get`）。
- **fetch_index 多通道**：CSI index-perf 主通道；价格指数断源时 **东财 push2his**（H30269/000300/932000）→ **腾讯**（000300）；全收益 H20269/H00300 无等价源。腾讯盘中 bar 按 `last_complete_day` 清洗。
- **fetch_etf**：东财主 / 腾讯兜底；`PREFER_TX=1`（CI）时腾讯失败会**回退东财**，避免单通道假绿。
- **gen_payload 引擎加载**：`sys.path.insert(0, engine)` 再插 `engine/backtest`，用 `EBT.__file__` 断言防顶层 engine.py 遮蔽（engine/ 下无顶层 engine.py，检查保留为防御）。
- **发布**：HSK 文件托管，`data/hsk-resource.json` 持久化资源（url=https://hci3bx.gicf.fun，resource_id=1789445817900755204）；无变化跳过；403 11301002 自动创建新资源。secrets：`HSK_API_KEY`。
- **⚠️ HSK URL 漂移**：HSK 的 update function 已被禁用（403 11301002），内容有变化时必须建新资源 → URL 会漂移（2026-09-15 已从 jjhujm.gicf.fun 变为 hci3bx.gicf.fun）。旧资源仍可访问但内容冻结。验证步骤以 `data/hsk-resource.json` 的最新 URL 为准。
- **ECharts 必须本地化**（jsdelivr CDN 在 WebView 挂起 60s 超时）；隐藏容器（offsetWidth=0）初始化图表失败 → 懒初始化。
- **红涨/绿涨语义隔离**：ETF 红涨、股票引擎绿涨（`.pzone` 作用域隔离 CSS 变量）。
- **天气视图注入链路**：`weather_tab.html` 含 `var WEATHER = /*__WEATHER__*/{};` 注入点 → build_portal.py 先 `replace("/*__WEATHER__*/{}", weather_json)`，整体片段再替换 template 的天气视图占位注释 → index.html。weather.json 缺失时降级为缺失提示（不报错）。双占位符残留校验含 `__WEATHER__`。
- **⚠️ 片段内注释勿写字面占位符**：weather_tab.html 头部注释曾写 `<!--__WEATHER_VIEW__-->`，注入后残留校验误报（`__WEATHER_VIEW__` 是 `__WEATHER__` 前缀），已改为中文字面描述。
- **天气 K 线懒初始化**：`.w-det`（details）闭合时容器 `offsetParent=null` → `ensureChart()` 先判 `offsetParent` 再 `echarts.init`；`WeatherEngine.show()` 仅在路由到天气视图后触发 resize；实时兑底用 `qt.gtimg.cn` JSONP（`window["v_secid"]` 字符串解析 `f[3]/f[4]/f[32]`），失败仅提示不阻断。
- **⚠️ qt.gtimg 为纯数据脚本不会主动回调**：`loadLive()` 需 `setInterval`（800ms×5）幂等轮询 `parseLive()`，并配 4s 超时 / `onerror` 仅提示；`WeatherEngine.show()` 里 `__weather_live_loaded` 标记兜底补齐（防视图隐藏期间错过 JSONP 返回）。

## 6. 本地开发环境

- Python：仓库根 `.venv`（python3.14，含 pandas/pyarrow/duckdb；**系统 python3.9 无 pyarrow 不可用**）
- 依赖：`requirements.txt`（pandas/pyarrow/duckdb/requests/numpy）
- 日常：`source ../.venv/bin/activate && python3 scripts/fetch_index.py && ...`；引擎单测：`cd engine && python3 -m unittest discover -s tests -v`（64 项）
- 网络：github.com 直连间歇超时 → `git -c http.proxy=http://127.0.0.1:7897 pull`

## 7. 本次代码整理（2026-09-15，commit bffe35c）

1. **三仓库合并**：red-dividend-strategy 引擎代码迁入 `engine/`（扁平模块集，BASE 路径自适应），gen_payload 路径从 sibling 改为仓库内；portal.yml 删除额外 checkout；stock-factor-engine 已无引用（选股功能被 payload/stock.json 吸收）。
2. **删除不起作用代码**：`scripts/migrate.py`（一次性迁移，已完成使命）；stock-factor-engine 本地目录与 GitHub 仓库归档；red-dividend-strategy 旧 index.html/模板/每日产物不入迁。
3. **退役旧链路**（用户确认"全面退役"）：red-dividend-strategy 旧 08:00 daily.yml 停用（旧 URL 945q5w.gicf.fun 不再更新），GitHub 仓库归档；stock-factor-engine daily-update.yml 停用（最近 4 次已 cancelled/failure 的死链路）。
4. **薄弱点加固**：单仓库消除引擎 sibling 依赖（引擎升级不再跨仓同步）；拉取失效备案已在数据融合轮完成——股票/ETF 腾讯兜底通道、sector rebuild_base 归档回退、fetch_stock/fetch_snapshot 失败不阻断整条流水线、verify 发布后校验。

## 8. 已知问题 / 薄弱点 / 后续

- **股票 hfq 缺口 105 只（B 型）**：腾讯数据源本身断档/停牌（三主机一致无 hfqday），非代码/WAF 问题；`fetch_stock` 每日重试预期持续失败（不触发退避，无副作用）。
- **morning 快照**：11:30 抓取失败时回退最近快照（7 天保留），页面照常展示。
- **CSI 全收益（H20269/H00300）仍无备用通道**：仅中证官网；价格指数 H30269/000300/932000 已加东财兜底，000300 另有腾讯。失败时保留昨日数据，**check_health 收盘后缺当日即红**（防静默腐烂）。
- **HSK URL 漂移**（见 §5）：update function 被禁用，内容变更即建新资源，门户 URL 可能不定期变化；`data/hsk-resource.json` 为准。
- **cn10y 十年期国债缓存**：`engine/backtest/cn10y_daily.csv`（⚠️ `refresh_cn10y.py` 曾写错到 `engine/cn10y_daily.csv`，已修）；东财 HTTP 主通道 + akshare 回退；portal/mirror 软刷新，健康闸门滞后 >10 天红。
- **因子层不入库**（每次重算）：如需历史因子回放需另行持久化。
- **engine/ 内 hs300_update.py 等离线工具**：CI 不再调用（gen_payload 内置沪深300 变体），保留供本地回测/体检。
- **qlab 段依赖外部报告**：quant-lab 报告为本地产物，CI 回退 `data/qlab/` 归档（懒更新）。
- **verify 覆盖 weather（2026-09-15 已补齐）**：天气数据在 `var WEATHER = {...}` 内联 JS 而非 PAYLOAD script，verify 用正则提取并对比本地/线上 weather data_date，不一致即红；HSK 发布 skip 判定仍以三端 data_date + content_sha 为主，weather 不单独参与 skip。

## 9. 大盘天气 · 四层择时（2026-09-15 新增）

门户顶部新增第四 tag「大盘天气」，展示 5 大宽基指数（上证 000001 / 深成指 399001 / 沪深300 000300 / 创业板指 399006 / 中证2000 932000）的四层择时信号。

- **数据链路**：`scripts/build_weather.py`（预计算四层策略，Python 复刻原 ai-timing-backtest 口径，FIXED=R0=1.0/fuseMode=blendC/fuseW=0.5/vpLevels=4/vetoMode=any/squeezeVeto/sqVRTHIN=0.8/sqFilterMA=20）读取 `kline/index/` 日K parquet → `data/payload/weather.json`（rows 全量 K 线 + ma/vp/fu 序列 + 最新 w 天气字段）→ build_portal 注入。
- **前端**：`portal/weather_tab.html`（`.wzone` 作用域样式独立，不影响其他 view）：指数天气卡片（晴/多云/阴/雨/观望，红涨绿跌）+ 逐项四层信号明细（均线状态机/量价网格 C/粘合清仓/融合决策）+ ECharts K线+MA+成交量（dataZoom 默认近 40%）+ `qt.gtimg.cn` JSONP 盘中实时兑底。
- **取数**：指数日K 由 `fetch_index.py` 双通道维护（CSI + 腾讯 fqkline），需保证锚定 5 指数在 `kline/index/` 有 T-1 完整 bar；中证2000（932000）走中证官网 index-perf 通道。
- **回退**：weather.json 缺失 → 页面显示"天气模块未打包/数据缺失"降级提示；build_weather 失败 → CI `|| warn` 沿用旧数据。
