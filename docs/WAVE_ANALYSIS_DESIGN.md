# ElliottWaver 收盘分析与概率评估实施设计

本文保留 A–G 的实施规格、依赖关系与验收用例，编号对应[产品路线图](ROADMAP.md)。下文的伪代码和字段示例用于说明设计。当前命令、参数和输出以[中文使用手册](USER_GUIDE.md)为准，交付和实测范围见[实现与验收状态](IMPLEMENTATION_STATUS.md)。

## 规则正确性验收

先验收数浪与日用摘要。对每个规则缺陷，记录模板、阶段、端点与 K 线、采用的规则版本，以及可独立计算的预期结果。用下表定位修改和回归范围。

| 核查对象 | 代码与验收方法 |
| --- | --- |
| 模板和硬约束 | 在 `engine/templates.py` 与 `engine/rules.py` 核对模板适用条件、硬约束、进行中阶段的待确认状态。正例、反例、边界值和涨跌镜像都给出独立预期，遇到流派差异先记录采用的口径。 |
| 拐点和结构证据 | 在 `engine/zigzag.py`、`engine/matcher.py`、`engine/parents.py`、`engine/subwaves.py` 核对极值、确认时间、父子浪位置和子浪证据。固定数据与复权口径做逐前缀回放，保留进行中端点和待定证据的状态。 |
| 条件价和排序 | 在 `engine/projector.py` 独立复算确认、硬失效、阶段退出与目标区，覆盖等价位、涨跌方向和跳空触发。在 `engine/scorer.py` 核对指南评分与候选权重，权重用于阅读主备结构。 |
| 日周线和报告 | 通过 `elliottwaver analyze`、`elliottwaver brief` 和冻结回放交叉核对同一候选的端点、规则、条件价、数据截止和图示。行情出现新极值或失效时，核对报告如何更新，同时保留原报告供查询。 |

合成行情用于验证明确的规则和边界。冻结真实行情用于验证接线、可解释性及多解场景。达到路线图的日用验收条件后即可收口。E 的统计概率使用自己的准入标准。

## A　把已有引擎变成一页可读的报告

### 入口、代码位置和执行顺序

新增 `elliottwaver brief SYMBOL`，保留 `elliottwaver analyze` 的单周期命令和输出契约。第一项交付以沪深个股和已接入的指数为范围，主周期为日线。接受 `--as-of`、`--anchor`、`--snapshot`、`--offline`、`--allow-gaps`、`--config`、`--output-dir`、`--open`、`--json`。离线需要日线源快照；实际输入周期不符报参数错误。手动锚点遵循原入口的精确匹配语义。

| 位置 | 实现 |
| --- | --- |
| [cli/main.py](../src/elliottwaver/cli/main.py) | 增加 `run_brief`，Typer 与 argparse fallback 同时注册。提取与 `run_analyze` 共用的请求／快照装载代码，沿用 `route_symbol`、`parse_as_of`、`fetch_bundle` 和 `SnapshotStore`。`--json` 的 stdout 只写结构化结果。 |
| [service.py](../src/elliottwaver/service.py) 与新增 `brief.py` | 前者保留正式数浪入口。后者接受已装载的日线源快照，组合摘要，签名见下。所有 CLI 和回放走同一个构造函数。 |
| [engine/span_selector.py](../src/elliottwaver/engine/span_selector.py) | 在实际选择锚点的位置产生结构化说明，保留原 `select_anchors(...) -> (dates, warnings)` 接口作为同一实现的投影。 |
| 新增 `schemas/brief.py` | 从 [Contract](../src/elliottwaver/schemas/market.py) 派生严格模型，复用 `AnalysisResult`、`LevelCondition`、`PriceZone`。保留旧 `analysis.json` 的 schema 1.0 和历史摘要校验算法。 |
| [visualization/report_formatter.py](../src/elliottwaver/visualization/report_formatter.py) 与 [cli/console_render.py](../src/elliottwaver/cli/console_render.py) | 新增摘要发布和终端摘要。各渲染器读取同一个 Brief 对象。复用 [render_html](../src/elliottwaver/visualization/chart_builder.py) 生成可离线查看的原图。 |

```python
# 新接口示意。source 在一次请求中只装载一次。
build_brief(request: AnalysisRequest, source: MarketDataBundle, settings: Settings) -> BriefRun
publish_brief(run: BriefRun, root: Path) -> Path
```

`BriefRun` 是内存对象，含待序列化的 `brief`、各周期 `AnalysisResult` 与一一对应的已准备 bundle。按请求调用 `prepare_bundle(req, source)`，再走与 `AnalysisService.analyze_bundle` 相同的计算。可把服务内部拆为“prepare + 已准备输入上的计算”，保留旧公开方法。对每个周期断言 `result.input_snapshot_id == prepared.snapshot_id`，再出图。保存源快照和真正参与各周期计算的快照。不要用聚合后的输入 ID 填源快照字段。

实现顺序可直接照以下数据流编写。第一项交付只需要 daily 一支，后续加入 weekly 时复用相同函数。

```python
prepared = prepare_bundle(request, source)
result, anchor_details = service.analyze_prepared_with_context(request, prepared)
window = describe_window(prepared, result, anchor_details)
cards = select_cards(result, prepared.raw_bars)
daily = summarize_period(result, window, cards)
```

这里的四个新函数名为拟定接口。现有公开的 `analyze_bundle` 在内部使用同一计算并只取 result。把旧实现中的过滤、认证、打分、Top-K 和 digest 计算原样移到共用路径，用单周期回归锁定行为。

### 数据契约与选窗

下表为 `brief.json` 的 v1 字段。A 先实现核心字段，B–E 的扩展采用可空字段或带版本的新 schema；旧文件的内容摘要始终按其写入版本复算。

| 字段 | 类型与约束 |
| --- | --- |
| `schema_version, brief_digest, source_snapshot_id, symbol, as_of, mode` | 版本、SHA-256、原始日线快照 ID、规范代码、带时区的决策时刻、`LIVE/FROZEN_DATA_REPLAY`。另存 `engine_version, config_hash, brief_policy_version`。 |
| `daily, weekly` | 周期摘要含 `status, market_asof, input_snapshot_id, result_digest, price_coordinate_id, window, cards, omitted_weight`。`daily` 必填。可用性另设 `weekly_state = NOT_REQUESTED / AVAILABLE / INSUFFICIENT_DATA` 与原因，A 默认未请求。 |
| `window` | `start, end, bar_count, anchors[], fallback, primary_pattern_start`。每个 anchor 含 `date, origin, background_timeframe, confirmed_at?`。origin 取 `EXPLICIT / MACRO_HIGH / MACRO_LOW / FALLBACK`。 |
| `cards[]` | 每条含 `scenario_id, original_rank, structure_key, role, difference`。通过 `scenario_id` 引用同目录分析 JSON 中的阶段、浪级、方向、权重、证据、条件和目标。role 取主选或备选。不能通过显示顺序反推原图的候选编号。 |
| `window_comparison, cross_timeframe, previous, forecasts[]` | 后续扩展。首次无历史时 previous 为空。无批准模型时，预测项为 `UNCALIBRATED` 且数值为空。 |
| `issues[]` | 结构化 `code, scope, message`，记录证据缺失、窗口回退、搜索截断、适用范围等。保留引擎原始 warnings。 |

1. 在现有背景拐点映射到当前周期的循环中保存其来源和 `confirmed_at`。在 A 的组合计算中同时返回这份溯源信息，避免渲染器重算背景拐点。选择逻辑、数量上限、高低点次序和回退索引与现有引擎共用。最终日期序列必须等于 `result.diagnostics.anchors`。若同日有多条来源，按来源类型和确认时刻稳定排序保留。`--anchor` 单列为 EXPLICIT。
2. 摘要的实际搜索范围从 `min(diagnostics.anchors)` 到 `prepared.bars[-1].session_date`，含两端，根数按实际 K 线数。单列主选第一个已完成浪标对应的 K 线日期为 `primary_pattern_start`。例如锚点是 2 月 1 日，首选从 3 月 5 日开始，报告同时列出两天。少于 252 根时回退只显示实际根数；短于 `require_history` 的下限走数据不足。
3. 主选沿用正式结果首位。在 `result.scenarios` 中按原顺序取同浪级、不同结构的最多两个备选。结构按 `(pattern_type, stage, pattern_direction, labels_and_raw_points)` 去重，其中 `labels_and_raw_points` 是已完成浪标、端点类型和 `prepared.raw_bars[pivot.index].bar_id` 的有序序列。`structure_key` 对该元组加规范代码与周期取 digest。暂定 tip、权重、复权价格不进入此键。跨浪级候选可在详情显示。每个备选按“方向 → 模板／阶段 → 第一个不同端点”顺序写出首处分歧。保留引擎权重及 omitted_weight，另列摘要省略但已进入引擎 Top-K 的个数。
4. 每张卡标明 `pattern_direction` 和 `next_leg_bias`，复制质量、证据覆盖、子浪认证状态和条件的 `kind/side/op/price`，价位标注坐标及数据截止日。给出第一个可行目标区和有效条件。`CONFIRMATION`、`INVALIDATION`、`STAGE_EXIT` 使用独立标题。某类条件或目标为空时显示“当前没有该条件／目标”。进行中端点标为暂定。高分和高权重各有自己的字段。
5. `UNCLEAR` 可带低清晰度候选或空列表。`SEARCH_LIMITED` 标明搜索不完整。主输入不足或陈旧沿用退出码 3，源失败 4，导出失败 6，非法参数 2。已有结果但不可用时可写带状态的诊断报告；引擎在产生结果前抛出异常时返回原错误码和原因，保留已有成功记录。`--open` 只在报告发布成功后打开页面。

### 交付文件

在 `reports/<market>/<symbol>/brief/<market_asof>/<brief_digest>/` 发布 `brief.json, daily.json, index.html, daily.html, report.md, manifest.json`。B 有可用周线时加入 `weekly.json, weekly.html`。首屏可直接读摘要，图表用同目录相对链接，全部可离线打开。页面转义来自行情或配置的文本。digest 绑定业务字段、日周结果摘要、源快照、有效选项、展示版本、模型和对比来源，排除自身及随机运行 ID／生成时间。manifest 记录文件集合、逐文件 SHA-256 和源／衍生身份。用 staging 目录生成并校验后原子发布，复用已有目录时验证业务身份及文件哈希；并发写相同路径要加同一把锁。A 不依赖历史索引。旧入口使用自己的报告目录。验收用例见文末表。

## B　周线背景和另一种起点

从相同的日线源快照派生 `1d` 和 `1w` 两个请求，保留相同 as_of 和 end。周线请求清空日线专属的手动 anchor，由 `prepare_bundle(weekly_request, source_daily)` 处理。该路径会在决策截点先准备日线，再聚完整周并处理周末复权基准。保留完整交易日历，周中决策不使用本周未结束的 K 线。周线结果、图和条件均用自己的准备输入，避免跨坐标比较绝对价位。

- 两边状态均为 OK 且主选下一段方向已知时，方向相同标 `ALIGNED`，相反标 `CONFLICT`，其余标 `UNDETERMINED`。分别显示周期、截止日和方向。不要凭跨周期同向推断已认证父子浪。周线历史不足时保留日线和具体原因；证券身份、源完整性或内部异常仍按错误处理。
- 自动选窗敏感性检查只做一次。若主分析有至少两个不同锚点，以最晚锚点作为另一个搜索下限，用原日线源快照、同一 as_of 和配置重跑正式服务，设置 `start=该日期, anchor=None`。这样会保留预热，并沿用自动窗口的“允许在起点之后起浪”语义。记录重跑实际最早锚点、根数和首选键。手动锁定或有效范围相同标 `NOT_APPLICABLE`。无可比首选或搜索截断标 `INCONCLUSIVE`。其余依次判定 `SAME_COUNT / CHANGED_COUNT_SAME_BIAS / CHANGED_BIAS`。此重跑只作敏感性说明，正式日线仍是原结果。
- 在 `brief` 中默认开启周线及换窗复核。A 独立发布时这两项显示未请求。B 的验收要比较独立 `analyze` 的日、周业务结果和联合报告两侧内容。

## C　旧判断、变动与修订

新增 `elliottwaver history SYMBOL [--as-of YYYY-MM-DD] [--mode live|frozen] [--json]`。以新的本地 SQLite 表索引已验证的 brief 目录。至少存 `brief_digest PRIMARY KEY, lineage_key, market_asof, as_of, observed_at, source_snapshot_id, report_path, mode`，对 `(lineage_key, market_asof, observed_at)` 建索引。lineage 按规范证券、feed、复权方式、周期组合、配置、锚点策略和引擎／摘要策略版本生成。模式单列过滤。

- 比较前从索引取同一 lineage 和 mode、日期严格早于本次行情日且当时已经发布的最近报告。live 以实际保存时刻约束当时可见的版本；历史回放从自己的运行批次按决策日期构造前序。固定 `previous_digest` 后计算本次身份。同日新修订作为另一版本保存。按日期查询返回该日各版本及时间，不把事后回放写成当时实发。没有上次记录时显示“首次记录”。
- 用稳定结构键匹配候选。键相同但暂定点移动记 `TIP_MOVED`。已完成原始端点序列延长、模板方向相同且前缀吻合记 `STAGE_ADVANCED`，多个可行匹配时报告歧义。原备选成为主选记 `ALTERNATE_PROMOTED`，其余主选变化记 `COUNT_CHANGED`。上一报告的确认／失效线按 D 的决策日坐标检查新增交易会话。
- 对两次源快照重叠的决策前历史，核对原始 bar ID 与按生效日期对齐的因子值（只比较共同覆盖区间）。发生回修标 `DATA_REVISED`，只有新增 K 线才归为行情推进。配置或引擎变化可列出版本差异并标 `NOT_COMPARABLE`。不同价格基准下用同一基准换算后才展示差额。
- 文件校验成功后再提交索引事务。重复 digest 用幂等插入；中途失败或索引丢失可从校验通过的 manifest 重建。比较与发布用同一 lineage 锁串行化；早期回放和无效结果均不能覆盖较新的有效实发记录。

## D　共用决策入口，独立结算结果

新增 `evaluation/decisions.py`、`evaluation/outcomes.py` 和薄命令 `scripts/evaluate_briefs.py --protocol FILE --output-dir DIR`。复用 [replay](../src/elliottwaver/evaluation/replay.py) 的前缀请求语义。其现有迭代按存在的 bar 且满 60 根才开始，评估器需按协议的交易日历构造**完整网格**，再调用 B 的无渲染计算入口。这样短历史、整日缺 bar、无候选、搜索受限都能进入分母对账。正常回放不写正式报告、历史索引或模型指针。

**协议输入。** 将已填写的协议签入仓库。必填 schema、事件版本、代码／有效配置摘要、缓存根、标的及日线 snapshot ID、交易所日历版本、分析与结果覆盖范围、采样频率、缺口策略、训练／校准／留出边界、统计门槛和随机种子。先用上证指数与茅台对账，再用约 30 只分层沪深个股及 4 个代表指数。保存名单来源和退市、覆盖不足处理。既有三角形样本用于开发回归；新研究单列未查看的留出。下例是可用于编写首个加载测试的样本条目。

```json
{"symbol":"600519.SH","asset_type":"EQUITY","source_timeframe":"1d","snapshot_id":"8378a32d2ce12a8419cb853ad4c6558aa4644f11aae7302c3319d14530d87f8d"}
```

顶层 `protocol_version="1"`，`samples[]` 使用上表条目，`grid` 含 `calendar_id, calendar_version, start, end, cadence, outcome_as_of`。cadence 取 EVERY_SESSION 或 LAST_SESSION_OF_WEEK。日线走势和候选确认默认 EVERY_SESSION，周线结构评估用 LAST_SESSION_OF_WEEK。`splits.train/calibration/test` 各含首尾交易日，`settings` 存有效配置，`events[]` 存事件 ID，`release` 存 E 的门槛、分档、块长和 seed。加载时检查日期有序、分段不重叠、事件版本已支持、样本代码与快照证券身份一致。只做 D 的结构和标签复核可以省略 splits、release；E 的训练入口要求两者完整。

**数据流。** 第一步只将决策当时可用的前缀交给分析和特征函数，保存决策与边界。第二步读取这些不可变记录与结果窗口行情，单独结算。交易日按交易所日历计数，`t` 为决策收盘，第一根结果 bar 是下一个交易会话。不要以“第 H 根有成交的 bar”代替第 H 个会话。指数保留原始点位。个股决策价 `C_t` 和 ATR 从 `prepared_daily.bars` 读取，未来行情调用 `adjust_to_basis(future_raw, factors, basis_date=t)`，即 `raw_s × F(s)/F(t)`。跨周 OHLC 先在同一基准调整各日，再聚完整周。周线的 `sessions` 含日历交易日，周线事件另构完整交易周网格。[复权实现](../src/elliottwaver/data/adjustment.py) · [周线聚合](../src/elliottwaver/data/resample.py)

| event_id | 锁定边界、单位及标签 |
| --- | --- |
| `CLOSE_BAND_5D_V1`、`CLOSE_BAND_20D_V1` | 单位为日线标的 × 决策日 × 期限，周线可作背景。用现有 `calculate(...).atr[-1]` 的日线 ATR(14)，要求有限且大于零。令 `L=C_t−ATR_t, U=C_t+ATR_t`。第 H 个会话收盘严格高于 U 为 UP，严格低于 L 为 DOWN，其他为 RANGE。途中触价不改标签。只需到期收盘和其复权证据，途中遗漏另记质量标识。 |
| `CONFIRM_FIRST_20D_V1` | 单位为日线候选 × 决策日。在 t 固定当时已经生效的 CONFIRMATION 与 INVALIDATION 列表，按原 side、op、price 求值，任何一条同类条件命中即触发该类。缺任一类标 UNDEFINED_EVENT。决策当根不参与结算，条件沿用原不等式，下个会话即使跳空越过边界也按该根实际 OHLC 比较。逐根检查，两侧同根命中为 AMBIGUOUS_SAME_BAR，仅确认先达为 1，先失效或完整窗口到期均为 0。首个未决会话缺失即停止定序。 |
| `TARGET_FIRST_20D_V1` | 单列检验日线原报告首个目标区与结构失效的先后。复用现有 [evaluate_event](../src/elliottwaver/evaluation/events.py) 的目标命中／跳空、同根双触及和失效比较语义，传入已对齐至决策日坐标的结果行情。缺目标或失效线为 UNDEFINED_EVENT。只有 STAGE_EXIT 时单列阶段结束。它不充当上一个事件的确认线。 |

决策时点超出有效输入覆盖记 INPUT_UNAVAILABLE。结算需用的会话已过去但缺 bar／因子记 INCOMPLETE_DATA，结果快照尚未覆盖到所需会话记 RIGHT_CENSORED。用协议的结果观察截止日与行情覆盖清单区分缺失和截尾。先触价事件若已观察到明确的首次命中，可以在期限到齐前核销。缺失、歧义、截尾都保留计数，并从可判定标签的分母剔除。预注册的同根歧义要另报全成／全败边界。历史三角形专用日 20／周 8 的对称 ATR 先触价事件保留独立编号及原协议。[三角形实验](reviews/2026-11-18-cn-triangle-experiment-protocol.md)

**交付格式。**

- `decisions.jsonl` 每个预定时点一行。`decision_id=digest([protocol_hash, symbol, as_of, timeframe, source_snapshot_id])`，带状态、排除原因、首选结构键、全部展示候选键、锚点、结果摘要和决策日价格坐标。
- `events.parquet` 每个决策 × 事件 × 候选一行，标的事件的候选键为空。保存 event_id、horizon、决策边界及其坐标、到期会话、状态、实际触发日和值。训练和展示都按同一发行频率结算，重复候选的新决策另列。另报按稳定结构键只取首次的研究结果。
- `summary.json` 按品种、周期、浪型汇总网格总数、可分析数、无信号、截断、发出判断和各种结果数。结构指标包括同一输入版本新增前缀后已确认拐点改写数、极值至确认的 K 线数、相邻可比主选的切换率。报告每百个可分析时点的触达数和仅已成熟可判定样本的条件命中率，零分母写 null。并列收盘方向的基本频率。
- `manifest.json` 锁定协议、源码、样本、有效配置和每份产物哈希。相同输入重跑后业务行逐行一致。比较完整输入与物理截断输入时核对决策内容，源快照 ID 可不同。

## E　短期走势概率和候选确认概率

新增 `evaluation/features.py`、`evaluation/calibration.py`、`forecast.py` 及训练命令 `scripts/train_forecasts.py --protocol FILE --decisions DIR --output-dir DIR`。训练可用单独的可选 research 依赖。终端推断用 NumPy 和经过校验的数值 JSON。训练阶段和在线调用共用同一特征变换和同一预测函数。

**特征 v1。** 按如下顺序存列名。价格组使用日线 `log(C_t/C_{t−20}), ATR14_t/C_t`。结构组使用日线正式合格候选中 UP、DOWN 和 UNKNOWN 的权重和、候选数、首选质量／100、首选证据覆盖率、前二质量差／100、首选下一段方向及最近完整周线首选下一段方向（−1、0、1）。候选确认事件另加该候选自身质量、覆盖、同级排名、模板与阶段 one-hot 及两个最近有效边界的距离／ATR。距离取绝对值，同类有多条取最小。全体合格候选权重从服务的 `weight_scenarios` 排序归一处产生一次，先取汇总再按 Top-K 截取，保留旧返回和业务 digest。全部方向权重来自同一次归一化，作为输入特征。候选为空时计数及权重和为零，缺测特征置零并带伴随的缺失标识。将类别字典、未知类及全部列顺序版本化。价格历史不足、源状态无效或搜索截断时输出原因并弃报。

**训练与决策。**

1. 每个 `event_id × market × asset_type` 独立训练。预先固定交易日切分，每段末端剔除结果期限跨入下段的决策，所有证券按共同日期切分。先训练多项逻辑回归（候选事件为二项），数值标准化参数只取训练段，零方差的 scale 取 1。工程初值为 L2、C=1、lbfgs、max_iter=1000，收敛失败记录状态。训练段缺任何目标类别则该模型不可发布。
2. 校准段最小化负对数似然。初版用标量温度，从固定集合 `[0.5, 0.75, 1, 1.5, 2, 3]` 选择损失最低者，平局取最接近 1 后取较小值。二项模型的两个 logits 表为 `[0,z]`，三项按已存类别顺序。推断为 `p=softmax((logits−max(logits))/T)`。同时训练三个对照，即训练集类别频率、仅 20 根动量、完整价格组，后两者用同样的校准流程。报告加入波浪特征相对价格组的差值。[逻辑回归接口](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html)
3. 留出段一次评估。多分类 Brier 为逐例 `sum_k (p_k−1[y=k])²` 再取均值；二分类统一用正类的平方误差。以“基线损失减模型损失”作为改善。按共同日期取连续时间块配对重抽样，携带块内全部证券／候选，块长至少覆盖事件期限。锁定块长、种子、次数、覆盖率、每类样本支持及分档参数。用可靠性分档核对平均预测和实际发生率。[概率校准与可靠性图](https://scikit-learn.org/stable/modules/calibration.html)
4. 将发布条件写入协议，先冻结再看留出。建议初值沿用既有设计：相对类别频率和动量两项基线，改善的配对 95% 区间下界均大于 0；对拟发布的类别／概率档，预测均值减实际发生率的 95% 区间全部落在 ±0.10 内。拟作为“波浪增益”发布时，价格组对照也须证实增益。例示的首版协议可用分档边界 `[0,.2,.4,.6,.8,1]`（左闭右开，末档含 1）、每展示档至少 50 个成熟观测且至少 10 个不同决策日期、重抽样 2000 次与固定 seed。记录唯一日期数、标的数和实际有效块数，无法估区间时记样本不足。线上三分类任何一个分量落入未获支持的档位，整组数值置空。公开发布结论应注明适用范围和报告覆盖率。
5. 训练产物 `model.json` 包含事件版本、品种／feed／复权策略、训练／校准／测试边界、适用引擎和配置摘要、特征顺序、编码字典、均值、scale、系数、截距、类别顺序、温度、各类支持档、样本量与验证摘要。`model_id` 对排除自身的模型内容取 digest。发布流程校验有限值、维度、类别集合、哈希和评估门槛，再原子更新本地批准模型清单。研究产物仅在批准后生效。回退到兼容的已批准版本，保留旧报告记录的 model_id。历史评估只装决策时已可用的模型；新模型重算旧数据须单列为回顾性研究。
6. brief.forecasts 的每项包含 `event_id, scope, horizon_sessions, scenario_id?, status, probabilities?, model_id?, valid_bins?, sample_count?, calibration_end?, reason?`。`status` 分为 APPROVED、UNCALIBRATED、INSUFFICIENT_EVIDENCE、OUT_OF_SCOPE、UNDEFINED_EVENT、MODEL_INVALID。只有 APPROVED 有数值。三分类按 UP/RANGE/DOWN 键输出，各项有限、在 [0,1] 且和与 1 之差不超过 1e−8。候选只输出 `p_confirm_first`，不同候选各自结算。旧 `WaveScenario.calibrated_event_probability` 是类型为 None 的旧契约，本次新增数值只在版本化 brief 中传递。模型缺失、不兼容或损坏时，基础分析仍可打开。

例如，尚无获准的 5 日走势模型时，以下对象可直接作为契约测试输入。可选字段缺省为 null。

```json
{"event_id":"CLOSE_BAND_5D_V1","scope":"SYMBOL","horizon_sessions":5,"status":"UNCALIBRATED","probabilities":null,"reason":"NO_APPROVED_MODEL"}
```

## F　浪型识别的标注与规则改进

标的涨跌和“判读者是否认可这个浪型”分别建标签。新增 `scripts/label_wave_cases.py export|score`。导出样本按证券类别、浪级、方向、歧义程度分层，包含无候选与拒绝样本。一个 case 固定证券、周期、决策时刻、同一浪级、原始端点范围和截至当时的 K 线。界面隐藏未来价格、模型首选、权重和其他人的判断。每个标注者提交 `case_id, annotator_id, selected_patterns[], abstain, reason`。允许多选、无法判定和“其他”。空集、弃权、缺交各有不同状态。

两名独立标注者按模板形成二元判读，分别报告每类样本量、正例率、原始一致率和 Cohen κ，分母为零报 null。保留争议个案。拟训练的目标为随机一位有效标注者认可该模板的概率，按 case 均权，在同一分割内保留该 case 的所有判读；多选用每类独立二项模型。发布标签必须写明“独立判读认可概率”，附一致率和盲标协议版本。首版建议以每模板至少 50 个双人有效 case、至少 10 个双阳性和 10 个双阴性、κ≥0.60 作为进入校准试验的门槛。先登记再看留出，类别概率另走 E 的留出误差检查，样本不足时只展示结构评分。若要新增 WXY 内部认证或升级 E 腿，从 D 的高频失败案例立项，补同级别人工证据、涨跌镜像和反例，升级规则版本，复验经典 A 组、五平台和冻结行情。结构证据的改善另行记录。

## G　自选、核销与维护

新增 `elliottwaver watchlist FILE --output-dir DIR [--json]`，FILE 为每行一个规范证券代码的 UTF-8 文件，空行忽略，按规范代码保序去重。每只调用同一 brief 流程，隔离错误，汇总 symbol、行情日、状态、报告路径、错误码。全部处理完后返回首个失败标的的非零退出码，UNCLEAR 按有效分析处理。增量核销使用 `(brief_digest,event_id,scenario_id或标的作用域)` 作唯一键，只推进未成熟记录，绑定实际结算行情版本。行情后来修订时新增结算修订版本，保留最初观察。模型撤回不改写已经发出的概率。交付一份支持矩阵，列出证券类别、周期、事件、适用模型、通过的测试和未通过原因。

## 验收用例与交接顺序

先在现有 [报告和回放测试](../tests/test_replay_and_reports.py)、[交易周期测试](../tests/test_timeframes.py) 的 fixtures 上覆盖 A–C。D–F 新建离线测试，用手算期望。将以下各行列为独立断言，功能完成后把命令、源快照／输入指纹、实测结果放入 [reviews](reviews/README.md)。

| 编号 | 输入或操作 | 必须得到的结果 |
| --- | --- | --- |
| A1 | 两只沪深个股、两个指数的日线。一组只剩一个合格候选，一组无候选。另测同码 `000001.SH` 和 `000001.SZ`。 | 四份报告各自可打开。代码、feed、原始与分析价格坐标匹配。卡片 ID、质量、权重、价位逐项匹配原分析。无合格候选为 UNCLEAR，无模型时数值为空。 |
| A2 | 固定 100 根且无合格背景锚点；另取 59 根；显式指定一个有效但无法匹配候选的锚点。 | 回退报告显示 100 根；59 根走数据不足、退出码 3；显式锚点不偷偷换窗。摘要起点与首选起浪日分别校验。 |
| A3 | Top-K 内含同方向不同模板、跨浪级候选、两个同结构键候选；分别去掉确认线和目标区。 | 同方向的不同模板能成为备选；展示别名映射回原图排名；保留同周期原始 omitted_weight；缺条件和目标时显示对应原因。模拟渲染失败后不存在半份已发布报告。 |
| B1 | 周中取日线前缀、节假日缩短周、周内发生拆股，再加一种周线不足的样本。 | 周中回到最近完整周，假日周按真实会话完成，拆股周按每日复权的 OHLC 聚合。日线／周线与独立入口相同。周线不足不阻断日线。 |
| B2 | 两个自动锚点的结果下一段相反；相同方向但结构键改变；显式锁定锚点。 | 换窗标 CHANGED_BIAS、CHANGED_COUNT_SAME_BIAS、NOT_APPLICABLE。替代窗口的分数不会覆盖日线主选。 |
| C1／G1 | 同一报告重跑两次、并发写、旧 as_of 后到、历史 OHLC 回修、因子回修、当日两份修订、导出中断。自选中插入一个失败代码。 | 幂等与版本查询正确，最初报告可读，变动归因可解释。无效记录不覆盖有效最新结果，其余自选标的完成。 |
| D1 | 决策收盘 100、ATR 2，分别设置第 5 个会话收盘为 103、102、98、97。中途先到 104 后到期收于 100。 | 依次为 UP、RANGE、RANGE、DOWN；最后一例为 RANGE。若到期日缺失且之后仍有数据，则 INCOMPLETE_DATA；截止尚早则 RIGHT_CENSORED。 |
| D2 | 决策日因子 2、收盘 100、ATR 2；第 5 个会话因子 4、原始收盘 51。 | 决策坐标收盘为 102，等于上界，标签 RANGE。用未来原始价 51 直接比较将使测试失败。指数同样行情用原始收盘。 |
| D3 | 规则为 HIGH > 104 确认、LOW <= 97 失效。首根 high=104、low=98；下一根 high=105、low=97。另测先确认、先失效、只有 STAGE_EXIT、完整窗口均未触及。 | 首根未确认；第二根同根歧义。其余依次为 1、0、UNDEFINED_EVENT、0。确认事件与原目标带事件可给出不同结果。 |
| D4 | 物理删除决策后的行情和后来生效的因子，重跑决策。用相同网格分别对账有缺口、无信号、搜索截断和结果未成熟。 | 当期窗口、稳定端点、候选、条件价、特征未变。改变未来价格只改变结算。汇总可从明细还原，零分母为 null。 |
| E1 | 三分类 logits 全零、T=1；二项 logits 为 [0,0]。删除模型、打乱特征顺序、篡改系数、换 feed、移出支持档。 | 分别输出三个 1/3 和 0.5。数值不合规或证据失效时返回对应非发布状态及空值，报告仍可读。离线导出与在线推断在 1e−8 内一致。 |
| E2／F1 | H=20 的开发行在校准段成熟；同一盲标 case 有两份判读；一类别所有人均弃权；构造模型相对基线无改善。 | 跨界行剔除，同一 case 全部落在同一分割，弃权不冒充否定。未通过的模型不能进入批准清单。 |
| 回归 | 五平台三角形及涨跌镜像、P3 父子位置、茅台日线和周线复权反例。 | 经典 A 规则与现有独立预期保持一致。茅台日线 2025-10-17 的目标先达日为 2025-10-24，周线 2024-07-19 的目标先达日为 2024-07-26；两例属于原三角形实验协议。 |

A1–A3 可使用以下冻结快照复验。

```bash
.venv/bin/elliottwaver brief 600519 --offline --snapshot 8378a32d2ce12a8419cb853ad4c6558aa4644f11aae7302c3319d14530d87f8d --json
.venv/bin/elliottwaver brief 000300.SH --offline --snapshot 9d87a58582943ad7145120c032ea8cc1505e9521084527b40cc4b18d30baebcf
```

上述 ID 对应已有本地实验样本。新环境先用现有 `elliottwaver data fetch SYMBOL --timeframe 1d` 保存日线并替换为实际返回的 ID。每个变更运行现有离线测试、Ruff 检查；触及引擎、回放或价格坐标时，再跑独立复验。

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/validate_cn_triangle_formal.py --output reports/brief-validation/formal.json
```
