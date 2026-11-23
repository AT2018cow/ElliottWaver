# ElliottWaver 0.6.0 实现与验收状态

更新于 2026-11-22。安装、日常操作和研究流程见[中文使用手册](USER_GUIDE.md)。目标规格见[详细实现方案](IMPLEMENTATION_PLAN.md)和[收盘分析与概率评估实施设计](WAVE_ANALYSIS_DESIGN.md)。

## 已交付范围

当前交付为可安装、可运行的 CLI MVP，并提前加入多周期主分析。计算核心不联网，行情获取、快照、分析、图表与回放分层实现。

| 能力 | 状态与证据边界 |
| --- | --- |
| 项目、配置、Schema | Python 包、Typer CLI、严格字段校验、配置优先级、错误码与结果身份已实现；快照 schema 为 1.1 |
| 沪深 A 股行情 | 新浪日线、因子、1/5/15/30/60 分钟；版本兼容与有期限的隔离进程；通过真实数据子集验证 |
| 沪深京指数行情 | 新浪指数日线（`SH000*`、`SZ399*`、`BJ899*`），日线/周线；无复权，`feed=sina-index`。配置 `data.cn_index_intraday_feed: tushare-cn-index` 后接入 Tushare `idx_mins`，按 8000 行上限和交易时段回补；真实权限样本待账户回执 |
| 美股行情 | Alpaca REST v2 适配器，显式 feed/adjustment/end、全部分页、资产 ID/类别/代码一致性核对、认证错误与重复 token 防护；日线/周线、DST、假期、提前收盘、完整周、raw/all 对齐和 SIP/IEX 分缓存已通过冻结响应与离线入口测试。AAPL、BRK.B、SPY 的真实 SIP 小样本和 AAPL 的 IEX 样本已通过，见[验收记录](reviews/2026-11-20-us-equity-acceptance.md) |
| 美股指数 | `SPX`、`NDX`、`DJI`、`RUT`、`VIX` 独立路由；配置 `data.us_index_feed: massive-index` 和 `MASSIVE_API_KEY` 后接入 Massive `I:` 指数聚合，保留无成交量、`volume_comparable=False` 和 RTH 会话过滤。假响应验证 5m 原生指数、日线背景和 workflow 路由；真实 Massive 指数权限待补 |
| 主周期 | `1m、5m、15m、30m、60m、1d、1w`，支持 `1h` 别名；两市场合成样本全覆盖，A 股真实样本全周期覆盖；CN 指数默认 `1d / 1w`，配置 Tushare 指数分钟后开放盘中周期；美股指数配置 Massive 后开放原生周期 |
| 时间边界 | 不纳入未完成/尚不可用 bar；UTC 与交易所会话分开；节假日、DST、提前收市、午休、小时线尾段与完整周处理已测试 |
| 复权 | 保存 raw 与因子；A 股基准日乘法复权；先复权日线再聚合周线；美股供应商 all 不进行二次复权 |
| 分析 | ATR/MACD、三尺度因果 ZigZag、背景锚点与回退、有限 CSP 搜索、端点与完整影线区间硬规则；`--long-term` 20 年窗口/首尾锚点/宽桥接搜索/子浪认证，`--allow-gaps` 显式跳过停牌缺口 |
| 浪型 | 普通涨/跌推动、ABC 锯齿、三角形、平台、楔形和双锯齿 WXY。父级上下文用端点和浪段规则认证。子浪使用更细尺度拐点和同一套浪段规则。已完成收敛三角形使用经典整组门槛：至少四腿锯齿族、复杂腿最多一条；E 腿嵌套只标记 `PROVISIONAL`。 |
| 评分与点位 | 分项证据、缺失状态、质量/覆盖/相对权重、Top-K 未展示权重、带方向条件、第五浪上限、合法目标带；完成态含 38.2%–100% 回撤目标，低分但硬规则通过的结构以 `EXCLUDED_STRUCTURE` 显式列出；第五浪同时比较浪 5/浪 1 与浪 5/浪 3，含 2.618/4.236 扩展位；按 K 线跨度标注浪级并提示 `MIXED_DEGREE_SCENARIOS` |
| 产物 | 单周期分析支持 Rich、JSON、Markdown、Plotly HTML、SVG/PNG 备选渲染，完整写入后更新对应周期/feed 的 latest。收盘摘要支持终端、JSON、HTML 和 Markdown，显示数据模式、截止时刻及候选规则证据，使用独立的摘要索引。 |
| 收盘摘要 | `elliottwaver brief` 首屏用中文显示主备浪型、当前浪、观察期限、目标区及改变判断的条件。「核对依据」集中展示分析范围、子浪与规则状态。默认日线及完整周线背景，长跨度自动选择周线。`--format json` 保留终端文字并只保存 JSON 文件。真实行情概率当前为 `UNCALIBRATED`。 |
| 盘中简报 | 明确给出盘中快照时自动采用其主周期；`BriefOutlook` 使用 `version=1.1`、`horizon_unit=BARS`、`horizon=5`，只选发行后尚未开始的 5 根主周期 K 线。首屏显示市场当地时间、最后一根已完成 K 线及可用时刻、推进/目标/失效条件和到期时刻。固定背景配对为 `1m→5m`、`5m→60m`、`15m/30m→60m`、`60m→1d`。旧盘中周线观察窗不再生成，结算返回 `SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH`。 |
| 短期预测增强 | PE-1 至 PE-6 核心：`project_with_gates` 条件角色、`BriefOutlook` 5 会话/2 周观察窗、推进/目标事件注册与评估、特征版本 4、A/B/C/D 对照、`freeze`/`score`、新事件模型作用域、按卡片概率展示、核销回放和终端/HTML/JSON 一致性；P-08 四标的（CN 个股/指数、US 个股/ETF）的 brief/watchlist/history/settle/support-matrix 全流程已通过；独立验收 PO-01 至 PO-09 已修复：推演条件语义、目标硬边界、发行/回放/核销共用冻结观察窗、训练/评分样本作用域、冻结与留出封存、标签语义、批量模型选择、终端精度和旧分析兼容；历史 holdout 已覆盖目标先达、推进和 2 周收盘事件，均正式记录为 INSUFFICIENT_EVIDENCE；旧评分产物只作回顾性研究留存，不用于批准。[实施记录](reviews/2026-11-21-prediction-outlook-protocols.md) · [独立验收修复](reviews/2026-11-21-prediction-outlook-review-followup.md) |
| 历史与自选 | `elliottwaver history` 查询已发布摘要，原摘要记录上次判断及变化，区分 `CONFIG_CHANGED`、`DATA_REVISED` 和 `NOT_COMPARABLE`。新增 `timeframe`、`feed`、`adjustment`、`last_bar_end_at`、`last_bar_id` 和 `last_bar_available_at`，同日盘中摘要按最后一根 K 线排序；`history --timeframe` 可筛选。`elliottwaver watchlist` 批量生成 JSON 摘要，支持有限重试、有界并发、模型注册表回退和单标的失败隔离。 |
| 结算与支持矩阵 | `elliottwaver settle` 使用本地快照，按 `(brief_digest,event_id,scope)` 去重并增量结算，行情修订追加版本，保留最初观察。盘中事件使用冻结的 5 个精确时隙和同一个观察窗解析器，记录触发 `bar_id`、起止时刻和价格口径；同根目标/失效、未知缺口和右删失分别保留状态。`elliottwaver support-matrix` 增加 5 个盘中周期和 5B 事件行，传入训练汇总后显示支持状态和原因。 |
| 离线评估 | `scripts/evaluate_briefs.py` 输出日线/周线/盘中 decisions/events/summary/manifest；支持收盘区间、确认或推进先达、目标先达事件；盘中协议使用 `EVERY_BAR` 和 `step`，决策 ID 含精确 bar 开始时刻，事件保留 `decision_at`、`due_at`、`period_starts`、`period_ends`、`trigger_bar_id` 和触发时刻；summary 按标的、资产类型、周期、浪型和期限分解，并包含已确认拐点改写数、极值至确认 K 线数、相邻主选切换率、预测覆盖、确认延迟和搜索截断；`scripts/merge_evaluations.py` 可合并单样本分批结果 |
| 多浪级引擎 | `engine_profile=hierarchical_v1` 显式启用 `hierarchical-rules-v3`。递归子浪选择按实际可证明的深度排序，历史日周关系也遵循请求深度。盘中新增 `engine/intraday_link.py`，按精确 K 线区间映射父结构腿到子结构，复用 `match_interval`、子模板许可、深度和共享预算。摘要包含日周/盘中关联与历史触发证据，评估使用正式摘要的首选结构键和比较模式。 [修复复验](reviews/2026-11-21-wave-engine-followup-repair.md) |
| 概率研究 | 30 只沪深个股和 7 个指数的 2026 留出检验中，8 项日线事件均未获批。修正训练入口后，指数 20 日目标先达的价格基线对照与校准均未过。2023–2025 开发期已完成滚动校准、目标几何和失效审计；C 按 A 参照入选，但相对含目标几何的价格基线无增量，波浪结构增益未建立。没有新增独立前向留出，状态为研究完成、待新留出，未注册模型。[留出重算](reviews/2026-11-20-cn-index-probability-recheck.md) · [开发期验收](reviews/2026-11-19-cn-index-target-accuracy.md) · [前向验收](protocols/cn-index-target-forward.md) |
| 端点复核 | `scripts/review_wave_cases.py` 用校验后的日线逐段复核盲标端点，输出规则证据。56 个案例中 18 个至少通过一个端点模板，38 个保留待定。[日用版验收](reviews/2026-11-19-release-preparation.md) |
| 标注研究 | `scripts/label_wave_cases.py export/import/score/direction/predict` 支持从已发布 brief/daily 自动生成分层盲标 case（含无候选、`EXCLUDED_STRUCTURE`/拒绝候选和歧义分层）、标的涨跌标签、标签导入、按 case 均权独立判读认可概率、双阳性/双阴性、原始一致率、Cohen κ、split 分组；`scripts/train_label_models.py` 训练每模板独立二项模型并接入 E 式留出校准和 50/10/10/κ≥0.60 门槛 |
| 快照与回放 | SQLite 索引、Parquet 不可变块、SHA256、原子写入、源身份检查；逐前缀回放与回放 manifest |

## 验证结果

以下为 `v0.6.0` 基线加本轮盘中增量后的验证结果。详细证据、快照 ID 和逐项命令见[盘中验收记录](reviews/2026-11-23-intraday-acceptance.md)。

```text
python -m pytest tests -q              553 passed
ruff check src tests scripts           PASS
ruff format --check src tests scripts  PASS
pip check                             No broken requirements found
```

测试在 Python 3.12 / Linux 环境执行。告警来自 `exchange_calendars` 调用 NumPy 的 timedelta 弃用接口。实际安装版本记录在 [requirements.lock](../requirements.lock)。

0.5.1 wheel 的复验使用当时的 `eliot` 命令；0.6.0 更名后对应命令为 `elliottwaver --version`、`elliottwaver doctor` 和 `elliottwaver demo`。

### v0.5.0 发布验收

`v0.5.0` 发布时全量测试 522 项通过。

0.5.0 验收使用冻结的 `600519.SH` 与 `AAPL` 行情检查中文首屏、目标显示与原始阈值。从冻结的 `AAPL` 和上证 50 复验日线短期事件与两周收盘事件，核对评估和核销状态、到期场次及行情摘要。前向评分入口使用合成同期发行记录测试，真实行情概率仍处于未校准状态。 [短期推演修复与复验](reviews/2026-11-22-prediction-outlook-pr01-pr04-repair.md)

### 早期发布复验

0.2.0 发布复验使用新浪的 `600519.SH` 和 Alpaca SIP 的 `AAPL` 生成实时摘要。冻结回归覆盖沪深个股及指数、美股个股及 ETF、SIP 和 IEX，核对 `brief` 与 `analyze` 的业务结果和报告校验值。[美股验收记录](reviews/2026-11-20-us-equity-acceptance.md)

测试包含手算 ATR/EMA、拆股因子方向、非交易日因子、同价不同日期保留、拐点确认前不可用、逐前缀不变性、涨跌两向的浪 5 百分比失效位和三角形 E 浪净回撤、影线合并违规、缺特征不增权、父级包含/精确腿/进行中父腿、5-3-5 子浪认证、区间外拐点不影响深度 2、分页与权限错误、快照篡改、导出中断、CLI 离线一致、事件同根歧义、时区等价、集合竞价、指数路由/无复权 provider/日线→周线/快照往返/分钟线拒绝及各主周期。美股另覆盖资产代码错配、分页尾页、raw/all 对齐、DST、假期、提前收盘、完整周、内部缺日、SIP/IEX 分缓存、离线入口和回放。

真实行情验证使用 **2026-11-16 已完成行情**。下列统计来自 v3 的[个股多周期](validation_20261118_ext5_v3.json)、[沪深京指数](validation_20261118_cn_index_v3.json)和[长历史](validation_20261118_long_history_v3.json)记录。当前 v6 的三角形结构认证、复权修复和同批小回放见[修复与决策](reviews/2026-11-18-cn-triangle-classic-v6.md)。原始快照和大图保存在本地 `.cache/live-smoke/`、`.cache/elliottwaver/` 和 `reports/`。

| 股票 / 周期 | 根数 | 输入起始 | 数据截止 | 分析状态 |
| --- | ---: | --- | --- | --- |
| 600519 / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |
| 600519 / 周线 | 255 | 2021-11-15 | 2026-11-13 | OK；排除未完成周 |
| 600519 / 小时线 | 340 | 2026-07-13 | 2026-11-16 | UNCLEAR；诚实保留低清晰度 |
| 600519 / 1 分钟 | 1970 | 2026-11-04 | 2026-11-16 | OK |
| 600519 / 5 分钟 | 1970 | 2026-09-10 | 2026-11-16 | OK |
| 600519 / 15 分钟 | 1360 | 2026-07-13 | 2026-11-16 | OK |
| 600519 / 30 分钟 | 680 | 2026-07-13 | 2026-11-16 | OK |
| 000001 / 日线 | 1214 | 2021-11-12 | 2026-11-16 | UNCLEAR；无达标候选 |
| 300750 / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |

沪深京指数与个股回归使用同一批 2026-11-16 快照。下表来自[指数验证摘要 v3](validation_20261118_cn_index_v3.json)。

| 指数 / 周期 | 根数 | 输入起始 | 数据截止 | 分析状态 |
| --- | ---: | --- | --- | --- |
| 000688.SH / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |
| 000300.SH / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |
| 000300.SH / 周线 | 255 | 2021-11-15 | 2026-11-13 | OK；首周收盘日 2021-11-19；日线 → 周线 |
| 399001.SZ / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |
| 899050.BJ / 日线 | 1101 | 2022-05-05 | 2026-11-16 | OK |
| 000001.SH / 日线 | 1214 | 2021-11-12 | 2026-11-16 | OK |
| 000001.SZ / 日线 | 1214 | 2021-11-12 | 2026-11-16 | UNCLEAR；个股回归 |

指数组均通过硬规则、候选权重、抽样前缀一致性和 JSON/MD/HTML 报告校验；指数结果标记为 `feed=sina-index`、`adjustment=none`、`factors=()`，不计算复权因子。

新增接入的[独立验收记录](reviews/2026-11-17-cn-index-acceptance.md)还包含当前源码下的周线手算、缓存隔离、旧快照兼容性，以及沪、深、京各一个指数的公开行情真实下载烟测。

股票九组与指数验证的七组（5 个不同指数的 6 组指数/周期组合 + 平安个股回归）均通过规则检查、候选权重质量检查、抽样历史前缀一致性以及 JSON/MD/HTML 产物校验。原始记录的单次纯计算耗时约 0.07–0.25 秒，不包括下载、快照 I/O 与出图；新版本耗时见对应验证 JSON。这不是全市场性能或完整 p95 基准测试。

长历史验证使用 `--long-term --allow-gaps`，下表来自 [长历史 v3 证据](validation_20261118_long_history_v3.json)。`--long-term` 现在默认启用深度 1 子浪认证；显式 `--subwave-depth 0` 可关闭。

| 标的 / 周期 | 根数 | 输入起始 | 数据截止 | 状态 | Top-K 候选 | 纯计算 |
| --- | ---: | --- | --- | --- | ---: | ---: |
| 000001.SH / 周线 | 1821 | 1991-01-02 | 2026-11-13 | OK | 8 | 4.15s |
| 399001.SZ / 周线 | 1646 | 1994-04-18 | 2026-11-13 | OK | 8 | 2.82s |
| 000300.SH / 周线 | 1263 | 2002-01-07 | 2026-11-13 | OK | 8 | 4.88s |
| 000001.SZ / 周线 | 1569 | 1994-04-18 | 2026-11-13 | OK；77 个允许缺口 | 8 | 2.02s |
| 600519.SH / 周线 | 1249 | 2001-08-27 | 2026-11-13 | OK；28 个允许缺口 | 8 | 6.49s |

长历史组均通过硬规则、候选权重、抽样前缀一致性和报告校验。5 组共 40 个展示候选中，父级 `VERIFIED` 12 个、`PROVISIONAL` 1 个、`UNKNOWN` 27 个；子浪 `VERIFIED` 3 个、`UNVERIFIED` 29 个、`UNAVAILABLE` 8 个。上证 Top-8 中包含 2008–2026、2013–2026、2019–2026 的三角形候选，浪级为 `SUPERCYCLE / CYCLE`，并出现 `MIXED_DEGREE_SCENARIOS` 提示。上证 2024–2026 双锯齿 WXY 获得 `CYCLE` 三角形 E 腿的 `EXACT_LEG` 父级关系，三条腿均通过子浪规则认证。

完成态目标扩展、第五浪评分修正与 `EXCLUDED_STRUCTURE` 可见性变更不包含按证券代码分支的逻辑。DeepSeek 初版的九组证据保留在 [validation_20261117_ext5.json](validation_20261117_ext5.json)。其中部分样本的业务内容未变，但由于引擎版本参与摘要，所有样本都生成了新的结果 ID；业务内容一致不能等同于结果身份一致。

第五浪扩展的历史证据见 [validation_20261117_ext5_fixed.json](validation_20261117_ext5_fixed.json) 和 [复验记录](reviews/2026-11-17-deepseek-ext5-fixed.md)。评分默认 `ew_score_v2`，可用 `ew_score_v1` 选择旧公式。共享引擎代码标识为 `ew-rules-v5/zigzag-v1/score-v2/subwave-v6/projection-v5/report-v4/search-v2/0.1.0`。增强引擎在此基础上加入 `hierarchical-rules-v3`，版本参与结果和报告身份。 [规则修复复验](reviews/2026-11-20-elliott-rule-conformance.md#修复复验) · [精确边界和多浪级复验](reviews/2026-11-21-wave-engine-followup-repair.md)

### 真实路径发现并修正的边界

1. 日历请求的边界可能落在国庆休市期。增加独立的日历构造缓冲，避免查询起点早于库返回的首个交易日；已有回归用例。
2. 新浪证券身份核验直接使用同源、带证券代码的公开报价响应，移除原先为一个股票下载全市场跨站列表的依赖。
3. 新浪 1 分钟样本每天在 14:57、14:58 起始的时隙没有成交 K 线。只在该源的现代沪深股票 profile 中，将这两个集合竞价时隙从“预期连续成交 bar”中排除，保留实际收盘 bar；其他缺口仍阻断严格分析，不补造价格。交易所规则将 14:57–15:00 定义为收盘集合竞价，集中撮合与连续成交不同。[上交所现行交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)、[深交所交易时间说明](https://www.szse.cn/www/investor/knowledge/stock/deal/t20191204_572383.html)
4. 周线不能把整周原始 OHLC 乘以周末因子。回放时也从原始日线重新复权后聚合，已有一周内拆股的独立手算回归用例。
5. 完成态目标原先只到 61.8% 回撤，价格越过该位后会显示无目标。现已扩展到 78.6%/100%，并把硬规则通过但质量分未达门槛的结构列为 `EXCLUDED_STRUCTURE`，避免它们在默认报告中完全消失；候选权重仍只在达标候选之间归一化。
6. 第五浪评分原先只比较浪 5/浪 1，偏好 1.0/0.618，延伸第五浪容易得零分。现同时比较浪 5/浪 1 与浪 5/浪 3，纳入 1.618/2.618/4.236 等标准扩展位，并对两种关系取较优拟合；这是评分口径修正，不针对单一证券。

## 与目标设计的阶段对应

| 阶段 | 当前交付状态 |
| --- | --- |
| M0 | 骨架、配置与契约实现并通过针对性测试 |
| M1 | 沪深真实子集通过；US 已完成资产身份、分页、会话、完整周、复权、离线入口和回放验收；AAPL、BRK.B、SPY 真实 SIP 小样本和 AAPL IEX 样本通过，未完成原计划 8–12 只跨市场真实样本验收 |
| M2–M5 | 核心 CLI 链路、规则、评分和报告已实现；次级分形认证按缺失证据处理 |
| M6 | 平台、三角形、楔形、WXY 支持端点级硬规则。父级关联和子浪认证共用浪段规则，内部认证深度最多两层。收敛三角形使用经典整组门槛；E 腿嵌套为 `PROVISIONAL` 候选。 |
| M7 | 已实现冻结数据逐前缀回放、日周线事件评估、基线对比、留出校准和模型注册机制。当前冻结样本的留出证据不足，尚未批准真实行情模型。 |
| W1–W2 | 未开始 Web；公共结构化结果与服务入口已预留 |

`max_subwave_depth` 默认 0，子浪证据显示 `UNAVAILABLE`。`--long-term` 默认设为 1，显式 `--subwave-depth 2` 再向下认证一层。全部父腿均已确认并通过位置规则和子模板硬规则时显示 `VERIFIED`。已完成收敛三角形还要求至少四腿为锯齿族、复杂腿最多一条；五平台或双复杂腿为 `UNVERIFIED` 且不获得满额子浪分。E 腿嵌套只标记 `PROVISIONAL`。证据不足或部分失败显示 `UNAVAILABLE` 或 `UNVERIFIED`。通用楔形的内部结构认证保留为 `UNAVAILABLE`。单周期候选的 `calibrated_event_probability` 保持 `null`，获批的短期事件概率通过 `brief.forecasts` 发布。

其他边界：BJ 个股仅有代码路由，线上接入显式拒绝；BJ 指数（`899*`）已接通。Sina 无可信停牌证据时内部缺口保持未知；长历史个股需显式 `--allow-gaps`。父级上下文只在本次枚举到的候选之间建立，搜索未枚举到的更大结构仍为 `UNKNOWN`；子浪认证覆盖 5 浪推动/楔形和 3 浪锯齿/平台，复杂组合调整、running flat、扩散楔形和截短五浪仍未覆盖。尚未接入 BaoStock 备源；指数分钟线未启用；分钟历史取供应商实际返回范围；评分默认参数为工程初值，未根据收益寻优。

## 复现

首先使用 `elliottwaver data fetch` 在本地保存快照。然后可用以下脚本对明确指定的快照重新生成证据和报告，全程不联网：

```bash
.venv/bin/python scripts/validate_snapshots.py \
  --cache-root .cache/live-smoke \
  --case maotai_day:1b89533b62b28eacc88ed5f95c7e4da2478679b2b7bb5a247d6ee111d5fee124:1d \
  --case maotai_week:1b89533b62b28eacc88ed5f95c7e4da2478679b2b7bb5a247d6ee111d5fee124:1w \
  --output reports/snapshot-validation.json
```

此命令要求对应本地快照仍在；新下载的行情即使价格相同，也可能因下载时间、修订及因子版本产生新 ID，应使用新命令返回的 ID。脚本保存源代码指纹、配置、输入与结果摘要。原始数据、代码规则、统计结果和实际交易验证属于不同证据层次，本次未进行交易收益验证。
