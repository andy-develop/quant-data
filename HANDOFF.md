# HANDOFF — quant-data 统一数据仓库与量化门户

> 更新：2026-09-15（代码整理：三仓库合并为单仓库，旧链路全面退役）
> 历史演进（2026-09-15 前）详见已归档仓库 `andy-develop/red-dividend-strategy` 的 HANDOFF.md（§1-§34），本文件为唯一交接主文档。

## 1. 项目概述

一个 GitHub 私有仓库（`andy-develop/quant-data`）承载全部代码与数据，服务一个**三合一量化门户**（https://hci3bx.gicp.fun）：

- **ETF 策略**（原 red-dividend-strategy）：红利低波（四态仓位机 v7.12+）、沪深300 择时（v8.0 变体）、行业轮动（v1.1，21 行业 × 32 ETF）
- **短线策略 / 个性化选股**（原 stock-factor-engine）：股票动量/波动因子（DuckDB 因子层）→ 选股列表
- **量化实验室**（外部 quant-lab 报告）：动量 + 黑盒，优先本地报告，回退 `data/qlab/` 归档

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
| payload | hl/hs300/sector/stock/morning | `payload/*.json` | 入库（门户输入） |
| qlab 归档 | 动量/黑盒报告 JSON | `qlab/` | 入库 |

当前数据规模（2026-09-15）：股票 hfq **3,610,087 行 / 5,227 只**（4,776 只完整 3 年）；指数均 10.7~13.0 年；ETF 2015→最新。`.git` ≈ 302M。

## 4. 双 GHA 任务（满足"12:00 更新、14:00 前出结果"）

1. **portal.yml**（`0 4 * * 1-5` UTC = 北京 12:00，75min timeout）：fetch_index → fetch_etf(PREFER_TX=1) → fetch_stock（`|| warn` 非阻断，mirror 已入库时秒级 no-op）→ fetch_snapshot（不阻断）→ build_factors → gen_payload → build_portal → commit data → **HSK 发布**（skip-if-unchanged：data_date 三端 + content_sha）→ verify 线上 data_date。
2. **mirror.yml**（`35 8 * * 1-5` UTC = 北京 16:35，120min）：收盘后镜像当日完整 K 线（fetch_index → fetch_etf → fetch_stock 主通道）→ housekeeping（**周一 `--compact`** 并入分片）→ 有变更才 commit+push。commit 先 `git pull --rebase` 防并发推送非快进。

## 5. 关键实现与踩坑记录

- **股票 fqkline WAF 经验**：`ifzq/web.ifzq` 双主机均曾被封 → **首选 `proxy.finance.qq.com`**（官方代理，实测 30+ 连发不触发）；主机池故障转移（proxy → ifzq → web.ifzq），被封主机 10 分钟回归。**FQ_MAX=800**（腾讯 >800 会截断到 640 根，不足 3 年）。降级探测 `backoff=False`，防 empty 股票误触发全局退避。
- **fetch_index 双通道**：CSI index-perf（T-1 完整收盘口径）+ 腾讯 fqkline（`last_complete_day` 清洗盘中半截 bar）。东财对 CI runner IP 连接级限流 → 弃用。
- **gen_payload 引擎加载**：`sys.path.insert(0, engine)` 再插 `engine/backtest`，用 `EBT.__file__` 断言防顶层 engine.py 遮蔽（engine/ 下无顶层 engine.py，检查保留为防御）。
- **发布**：HSK 文件托管，`data/hsk-resource.json` 持久化资源（url=https://hci3bx.gicf.fun，resource_id=1789445817900755204）；无变化跳过；403 11301002 自动创建新资源。secrets：`HSK_API_KEY`。
- **⚠️ HSK URL 漂移**：HSK 的 update function 已被禁用（403 11301002），内容有变化时必须建新资源 → URL 会漂移（2026-09-15 已从 jjhujm.gicf.fun 变为 hci3bx.gicf.fun）。旧资源仍可访问但内容冻结。验证步骤以 `data/hsk-resource.json` 的最新 URL 为准。
- **ECharts 必须本地化**（jsdelivr CDN 在 WebView 挂起 60s 超时）；隐藏容器（offsetWidth=0）初始化图表失败 → 懒初始化。
- **红涨/绿涨语义隔离**：ETF 红涨、股票引擎绿涨（`.pzone` 作用域隔离 CSS 变量）。

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
- **CSI 指数（H20269/H30269 等）无备用通道**：全收益指数仅中证官网发布，5 次重试 + 幂等增量；失败时 CI 红但仓库保留昨日数据，门户不发布残缺。
- **HSK URL 漂移**（见 §5）：update function 被禁用，内容变更即建新资源，门户 URL 可能不定期变化；`data/hsk-resource.json` 为准。
- **cn10y 十年期国债缓存**：静态 CSV（2013-2026），建议每季度手动 `python3 engine/refresh_cn10y.py` 刷新（需 akshare）；缺失时引擎补齐估值字段为 NaN，估值门大面积缺失流水线红掉。
- **因子层不入库**（每次重算）：如需历史因子回放需另行持久化。
- **engine/ 内 hs300_update.py 等离线工具**：CI 不再调用（gen_payload 内置沪深300 变体），保留供本地回测/体检。
- **qlab 段依赖外部报告**：quant-lab 报告为本地产物，CI 回退 `data/qlab/` 归档（懒更新）。
