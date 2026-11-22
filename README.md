# EliotBot

面向沪深个股、沪深指数以及美股个股和 ETF 的艾略特波浪分析程序，按“数据获取 → 多尺度拐点 → 候选数浪 → 规则校验 → 评分与点位 → 图表报告”组织能力。

**中文终端摘要直接显示主备浪型、当前浪、短期目标区和需要重新数浪的价位。** 日线观察接下来的 5 个完整交易日，周线观察 2 个完整交易周。需要查看单周期候选细节、盘中周期或长历史时，用 `eliot analyze`。CLI 也支持历史查询、自选批量和到期结算。 [版本说明](CHANGELOG.md)

当前版本为 **0.5.1**。已有环境可安装新版 wheel 或从源码升级。用 `eliot --version` 查看包版本，用报告中的 `engine_version` 核对分析规则。 [升级步骤](docs/USER_GUIDE.md#从已有安装升级)

发布验收以日线和完整周线为主。沪深指数使用 `sina-index` 无复权行情，例如 `000688.SH`、`399001.SZ`。美股样本覆盖上市个股和 ETF。两边的个股也有小时线和分钟线入口，美股盘中目前有离线回归。候选权重表示相对评分。完整范围见[实现及验收状态](docs/IMPLEMENTATION_STATUS.md)。

评分默认使用 `ew_score_v2`；配置 `scoring.profile: ew_score_v1` 可以保留原第五浪评分公式。评分版本参与配置摘要，结果中的 `engine_version` 末尾追加当前 profile，完整结果身份参与摘要。

首次使用请阅读[中文使用手册](docs/USER_GUIDE.md)，包含从启动到查看报告的完整步骤、多周期命令、结果解读、离线快照、回放和常见问题。

收盘报告、自动选窗和概率评估的完成标准见[产品路线图](docs/ROADMAP.md)。模块职责、数据契约和验收用例见[收盘分析与概率评估实施设计](docs/WAVE_ANALYSIS_DESIGN.md)。

美股日用版的 Alpaca 联调、规则验证与验收见[美股实施计划](docs/US_EQUITY_IMPLEMENTATION_PLAN.md)和[美股验收记录](docs/reviews/2026-11-20-us-equity-acceptance.md)。

## 安装

在源码目录使用 Python 3.11 或更高版本安装中美日用版。发布验证使用 Linux / Python 3.12。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[cn,us]'
.venv/bin/eliot --version
.venv/bin/eliot doctor --market CN
.venv/bin/eliot doctor --market US
```

开发和全量测试可用 `.venv/bin/python -m pip install -e '.[dev,cn,us]'`。`requirements.lock` 保存验证过的 Python 3.12 / Linux 依赖版本。[Wheel 安装](docs/USER_GUIDE.md#2-安装与环境检查)

## 先看离线演示

无需密钥或网络，所有演示图表会明确标记为合成行情：

```bash
.venv/bin/eliot demo
.venv/bin/eliot demo --symbol 600519 --timeframe 1h
.venv/bin/eliot demo --timeframe 1w
.venv/bin/eliot demo --renderer svg --format html,png,md
```

默认输出 `analysis.json`、`report.md` 和可离线打开的 Plotly `chart.html`，终端会给出目录及快照 ID。HTML 中可以切换候选剧本、缩放和查看拐点确认时间。SVG 渲染器可以直接生成 PNG；Plotly 的 PNG 导出还需安装 `.[png]` 和相应浏览器运行环境。

## 分析个股和指数

沪深 A 股和指数使用公开的新浪行情，无需 API key。先从收盘摘要开始。

```bash
.venv/bin/eliot brief 600519 --format json
.venv/bin/eliot brief 000300.SH --timeframe 1w --format json
```

在终端先核对行情日期和状态，再查看主备浪型、目标区、推进与失效条件。「核对依据」列出分析范围、子浪证据与规则通过情况。默认以日线为主，附最近完整的周线背景。指定周线、使用周线快照，或在自动模式下请求超过五年的跨度时，以周线作为主分析周期。`--format json` 保留终端文字且只保存 JSON 文件。要将终端输出改为结构化 JSON，再加 `--json`。

需要图表或逐条查看实测值时，可用 `eliot brief 600519 --open` 打开默认网页摘要，展开候选的「规则复核」。

需要逐腿结构和日周关联时，显式选择增强配置。样例配置的认证深度为 1，可用 `--subwave-depth 2` 再检查一层。 [增强模式用法](docs/USER_GUIDE.md#431-高级多浪级引擎实验)

```bash
.venv/bin/eliot brief 600519.SH --config config.advanced.example.yaml --open
```

需要查看单周期完整诊断、小时或分钟线时，使用 `analyze`。

```bash
.venv/bin/eliot analyze 600519
.venv/bin/eliot analyze 300750 --timeframe 60m
.venv/bin/eliot analyze 600519 --timeframe 5m --output-dir reports/my-scan
```

美股先复制 `.env.example` 为 `.env`，在本地填写 `ALPACA_API_KEY`、`ALPACA_API_SECRET`，按账户选择 paper/live `ALPACA_API_ENDPOINT`：

```bash
.venv/bin/eliot brief AAPL --feed sip --format json
.venv/bin/eliot analyze AAPL --feed sip
.venv/bin/eliot analyze MSFT --timeframe 1h --feed sip
.venv/bin/eliot analyze AAPL --feed iex --timeframe 15m
```

默认 SIP，权限失败不会换成 IEX。美股分钟/小时线采用 RTH 开盘锚定聚合；原生日线采用供应商日线口径，保守等到纽约日期结束并经过访问缓冲后再视为完整，和 RTH 收盘不是同一截止时刻。

A 股小时线不跨午休。周线从先复权的日线聚合，只分析完整周。新浪分钟历史长度随周期变化，不能保证任意历史日期都有分钟数据；当前 1 分钟样本约有 1,970 根，而不是完整 90 日。收盘集合竞价的已知无成交分钟时隙不补造 K 线，其他未知缺口会显示数据不足。

## 历史、自选与到期结算

```bash
.venv/bin/eliot history 600519
mkdir -p reports
printf '600519.SH\n000300.SH\n' > reports/my-watchlist.txt
.venv/bin/eliot watchlist reports/my-watchlist.txt --output-dir reports/watchlist --json
.venv/bin/eliot support-matrix
```

`history` 查询已发布的摘要。自选清单每行一个代码，`watchlist` 给每个标的保存 JSON 产物并记录失败。`settle` 使用原报告和覆盖到期日的行情快照核对收盘或触价结果。`support-matrix` 可读取模型训练汇总，列出各事件的支持状态。取数、核销、查看结果的命令见[中文使用手册](docs/USER_GUIDE.md#47-自选批量)。

仓库当前没有已批准的真实行情概率模型。可以独立运行[离线评估和校准](docs/USER_GUIDE.md#48-离线评估与概率研究)，或从报告开始[导出盲标案例并评分](docs/USER_GUIDE.md#49-盲标导出与一致性评分)。

## 快照、离线复现和回放

```bash
.venv/bin/eliot data fetch 600519 --timeframe 1d

# 将下面的值替换为上一条命令输出的 snapshot_id
SNAPSHOT_ID='替换为完整snapshot_id'
.venv/bin/eliot data inspect "$SNAPSHOT_ID"
.venv/bin/eliot brief 600519 --offline --snapshot "$SNAPSHOT_ID"
.venv/bin/eliot analyze 600519 --offline --snapshot "$SNAPSHOT_ID"
.venv/bin/eliot replay "$SNAPSHOT_ID" --start 2025-01-01 --step 5 --output reports/replay.jsonl
```

`analyze` 离线时默认沿用快照周期，`brief` 仍按自动规则选日线或周线。日线快照也可以通过 `--timeframe 1w` 生成周线分析。每份快照按内容寻址，Parquet 文件与元数据都校验 SHA256。回放使用同一引擎逐前缀计算，并写入带源快照 ID 和文件校验值的 manifest，模式为 `FROZEN_DATA_REPLAY`。

配置使用当前 [config.example.yaml](config.example.yaml)，通过 `--config config.yaml` 加载；支持 `ELIOT_ANALYSIS__TOP_K=5` 等环境变量覆盖。凭证只放环境或 `.env`。`--json` 将结构化结果写 stdout，错误与诊断写 stderr。

`UNCLEAR` 是正常的分析结果；主数据不足/陈旧返回退出码 3，来源或权限错误返回 4，必需报告导出失败返回 6。

## 验证与当前边界

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

验收覆盖波浪硬规则、个股与指数、多周期、冻结行情回放和报告完整性。各次实测的输入范围和结果见[验收记录索引](docs/reviews/README.md)。

默认模式枚举普通推动浪、ABC 锯齿，以及端点级的三角形、平台、楔形和双锯齿 WXY。父级认证要求端点吻合且子模板符合浪段规则，只有时间包含时标记 `PROVISIONAL`。子浪认证使用相同的浪段规则，逐腿报告 `VERIFIED / UNVERIFIED / UNAVAILABLE`；已完成收敛三角形还要求至少四腿为锯齿族、复杂腿最多一条，E 腿嵌套只标记 `PROVISIONAL`。

- [原始系统设计](DESIGN.md)：产品目标、核心思路和交互设想。
- [中文使用手册](docs/USER_GUIDE.md)：安装、实际操作、结果解读、配置及排错。
- [详细实现方案](docs/IMPLEMENTATION_PLAN.md)：外部实现参考、设计修订、数据契约、算法细节、开发任务和验收标准。
- [实现及验收状态](docs/IMPLEMENTATION_STATUS.md)：当前完成范围、验收证据、已知边界和复现方式。
- [个股多周期验证摘要 v3](docs/validation_20261118_ext5_v3.json)：输入/结果身份、覆盖区间、规则和前缀一致性。
- [沪深京指数验证摘要 v3](docs/validation_20261118_cn_index_v3.json)：指数日线/周线与个股回归的快照、状态和前缀一致性。
- [长历史验证摘要 v3](docs/validation_20261118_long_history_v3.json)：1991–2026 A 股长历史、父级上下文、子浪规则认证和浪级验证。

## 许可证

Copyright (C) 2026 AT2018cow.

本项目采用 [GNU General Public License v3.0](LICENSE)（GPL-3.0-only）。你可以自由使用、修改和分发；分发修改版时需按 GPL 提供相应源码并保留许可声明。第三方行情数据、依赖和商标仍受各自条款约束。
