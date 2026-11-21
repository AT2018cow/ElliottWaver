# 多浪级波浪引擎实施计划

> 初次独立复核的问题与结果见[独立验收](reviews/2026-11-20-wave-engine-codex-review.md)。当前修复情况见[修复复验](reviews/2026-11-20-wave-engine-repair.md)。

## 交接范围与发布节点

DeepSeek 负责实现和自测，Codex 负责独立验收。以本地 `0.2.1` 源码包及校验文件为基线，保留 `ew-rules-v4` 已验收的 E 浪净回撤、浪 3 百分比长度、浪 5 失效价。开始前记录基线源码指纹、快照 ID 和配置。`v0.2.0` 标签对应前一版，复验基线使用已包含两项规则修复的 `0.2.1`。 [基线规则复验](reviews/2026-11-20-elliott-rule-conformance.md#修复复验)

范围为沪深个股和指数、美股个股及 ETF 的日线和完整周线。小时线只作为后续可选的细分证据。继续使用 `eliot analyze`、`eliot brief`、`eliot watchlist` 和现有历史、报告、结算入口。新增结果使用结构证据与相对评分，事件概率继续走现有独立注册和校准流程。

| 交付节点 | 开发内容 | 可用状态 |
| --- | --- | --- |
| 日用增强版 | WE-1 推动与锯齿、WE-2 日周匹配、WE-4 小样本穷举和换窗复核、WE-5 主备演进 | 主备方案能展开成带证据的浪树，能定位日线属于周线哪一腿，能复盘主选为什么变化。 |
| 浪型扩展版 | WE-3 分批加平台、阻挡三角形、楔形、截短第五浪和组合调整 | 每个亚型有自有正反例、证据要求、状态与价位解释。 |
| 日用默认版 | WE-4 性能、搜索覆盖和去重，加上五项整体验收 | 先进模式达到下面的规则、因果、兼容性和耗时门槛后，单独决定是否切换默认。 |

## 公共配置和数据契约

- 在 `AnalysisConfig` 增加 `engine_profile: Literal["classic_v4", "hierarchical_v1"]`，默认 `classic_v4`。通过现有 `--config` 选择。新模式最初支持 `1d/1w`，其他周期请求明确回报支持范围。`SearchConfig.max_subwave_depth` 显式为 0 时只跑端点，1/2 分别验证这两层。高级模式的日用配置示例设置为 1，深度 2 用于复核。沿用 `--subwave-depth` 的显式覆盖和 `--long-term` 入口。
- 高级模式使用独立引擎标识和配置摘要。旧模式按 `0.2.1` 的业务字段序列化，默认新增配置项从旧配置哈希投影中排除。新 `AnalysisResult` 约定 `schema_version=1.1`，新 `Brief` 约定 `schema_version=1.2`。对旧报告按原始 JSON 校验散列，再填充模型默认值。现有 `Pattern` 大类和 `completed_waves`、`rules`、`conditions` 字段照常使用。高级模式输出新增 `structure`。旧版序列化只排除新增字段，原字段的 `null` 仍参与原摘要。发布、读取、历史、盲标导出、核销与评估入口应一起覆盖两种报告。概率兼容性检查取实际执行的引擎标识和配置摘要。
- 核心输入用 `prepare_bundle` 得到决策时已完成、已可用的行情，结构内每个端点须可追到该输入的原始 bar。`confirmed_at <= as_of` 才能作为已确认端点。`SEED` 和 `DEVELOPING` 可用于待定候选，对认证的边界不计作已确认。留存 `source_snapshot_id`、分析输入 ID、`as_of`、`feed`、`adjustment`、`price_coordinate_id`。结构节点、目标区与失效条件用同一已验证价格基准。
- 保留三种身份。`scenario_id` 定位本次候选，继续用于报告与结算。`structure_key` 用固定规范聚合同一时点的重复结构。`count_lineage_key` 依据市场、证券、周期、feed、结构模板、稳定起点和共同已确认前缀判断连续性，与 `HistoryStore` 的摘要查询键各自独立。至少有两个共同确认端点并匹配同一条已确认腿，才可沿旧前缀延续。最后一个进行中极值、评分、锚点来源和发现尺度都不参与跨时点匹配。若同一前缀兼容多个候选，保留歧义记录。

## WE-1 逐腿验证与结构树

**实现位置**为 `engine/subwaves.py`、`engine/templates.py`、`engine/rules.py`、`schemas/wave.py` 和 `service.py`。复用 `certify_subwaves` 的 finer ZigZag 输入、`allowed_child_templates`、`check_pattern`。增加比较指定区间的能力，供 WE-2 复用。只在高级模式启用新认证语义。

```python
match_interval(
    left: PivotPoint, right: PivotPoint, allowed: tuple[Pattern, ...],
    bars: Sequence[Bar], pivots_by_scale: Sequence[ZigZagResult],
    *, as_of: datetime, depth: int, budget: SearchBudget,
) -> IntervalMatch
```

1. 区间使用父腿首末端点，包括相同 bar ID、价格与高低类型。枚举严格落在端点之间、决策时已确认的更细拐点。用可复用的影线极值查询，验证更细拐点之间的每段 OHLC。允许跨过内部噪音，跳过的影线必须仍在选中端点的极值区间内。使用 `[left.index+1, right.index]`，沿用离开端点所在 K 线的先后顺序未知口径。
2. 对完整普通推动的 1、3、5 检查驱动结构，3 只能是 `IMPULSE`，2、4 检查相应调整结构。锯齿的 A、C 为驱动结构，B 为调整。通过 `allowed_child_templates` 共享许可表，锯齿或组合内部的调整分支由模板规则递归验证。嵌套必须严格缩短区间或降低可用尺度，并消耗深度和公共预算。组合分支要保留每个允许子模板的最优已证实结果，避免先找到一个子模板就丢掉能使全组通过的替代结构。
3. `WaveScenario.structure` 记录版本、验证状态、请求深度及已验证深度、节点、每条腿的结果和候选未获证据的原因。节点至少含 `node_id`、`pattern_type`、`variant`、`timeframe`、`direction`、`stage`、`points`、`rules`、`child_links`。点沿用 `PivotPoint`，附原始 bar ID 与价格坐标标识。腿至少含 `wave_label`、`left_id/right_id`、`allowed_templates`、`child_node_id`、`status`、`reason_code`、`failed_rule_ids`。用节点 ID 引用，终端摘要每腿输出一行，HTML 可展开，JSON 能复算全量证据。
4. 端点阶段与证据状态独立。已确认端点和要求深度的全部必需腿通过才记 `VERIFIED`。进行中、确认延迟或细数据不足记 `PENDING`，注明原因。预算耗尽记 `SEARCH_LIMITED`。已定位的父级位置与许可表冲突，或明确定义的搜索域完备且所有许可结构都违反确定规则时，才记 `REJECTED`。单纯未搜索到合法切分保留 `UNRESOLVED`。现有 `subwave_status` 做投影，完整证明为 `VERIFIED`，部分证据或搜索未完成为 `UNVERIFIED`，关闭或全无输入证据为 `UNAVAILABLE`。深度 2 缺证据时仍展示已验证的第 1 层。

**通过条件**。手工造完整 `5-3-5-3-5` 和 `5-3-5` 正例，主备 JSON 的每腿端点和规则可复算。构造相同外层端点但浪 3 内部完备地只能匹配 ABC 的反例，其父浪不能获认证。另测内部影线越界、跨腿拐点、进行中末腿、同根外包 K 线、证据少一层与预算截断。`test_engine.py` 保留原经典三角形整组测试。 [推动浪](https://www.elliottwave.com/waveopedia/impulse/) · [锯齿](https://www.elliottwave.com/waveopedia/zigzags/)

## WE-2 日周父子浪匹配

**实现位置**为 `engine/parents.py`、`data/periods.py`、`data/resample.py`、`service.py`、`brief.py` 和 `schemas/brief.py`。`build_brief` 已拿到日线 `AnalysisContext`、完整周线 `AnalysisContext` 和各自的 prepared bundle，新增 `link_timeframes(daily_context, daily_bundle, weekly_context, weekly_bundle, *, as_of, budget)`。保留 `cross_timeframe` 方向对照字段，新增 `wave_relations` 记录结构关系。`analyze` 保持单周期结果，`brief` 和 `watchlist` 调用同一关联函数。

1. 周线父点定位到聚合使用的日线区间。根据周高、周低及 pivot 高低类型收集实际贡献的日 bar，核对周线日历、完整周和周确认时间。A 股日周基准日可不同，按同一份源日线的已知因子将周价映回日线坐标再校验。美股核同一 feed 和聚合使用的 `provider_all`，沿用 `as_weekly` 的先复权再聚合。相等极值对应多根日 K 线时保留所有来源，无法唯一确认的边记为 `PROVISIONAL`。价格口径不可核实则记 `UNDETERMINED`。
2. 对当前日线主备各取最多 8 个已合格根候选，另保留实际已展示候选。周线取同样上限，从 `AnalysisContext.qualified` 取，独立于展示 `top_k`。先按已确认父腿的日线起止区间索引，再复用 `match_interval` 验证已结束的历史父腿。历史子腿用结束时确认的区间，允许确认时间晚于极值日且不晚于本次截止。`attach_parent_context` 的周期内匹配仍独立可用。
3. 每条边记录 `weekly_scenario_id`、`daily_scenario_id` 或区间验证得到的日线子节点、`parent_wave_label`、日周端点来源、`relation`、`status` 和规则原因。两端映射唯一且吻合、方向与该完整父腿一致、位置准入和所需子浪证据均满足时为 `EXACT_LEG/VERIFIED`。只有区间包含或父腿进行中为 `CONTAINED/PROVISIONAL`，即使日线当前下一段反向，也按实际子段位置解释。**判矛盾的前提是双方可比较且声称匹配同一条腿**，例如唯一且完全相同的端点上出现不允许的子模板，返回 `INCOMPATIBLE` 和失败规则。跨越不同父腿但未找到可匹配的具体位置、缺数据、搜索截断、未配对，使用 `UNDETERMINED`，附原因。
4. 先保留各周期本身成立的结构，再列可搭配的日周主备组合。组合优先显示已验证关系，再显示待验证关系，同级按原证据评分和稳定 ID 排序。不把方向对照结果直接写成结构结论。周线仍在发展时，后续日线可以作为未确认延伸单独展示，周线父端点仍来自最后一根完整周。

**通过条件**。冻结合成周线 `0/1/2/3/[4]=100/120/110/150/[132]`，其中日线下行 `0/A/B/C=150/139/145/132` 是第 4 浪的合法候选。周腿进行中时应为 `PROVISIONAL`，周腿和子浪证据齐备时升级。另测周浪 2 中的独立三角形、同区间错误方向、两种父候选、周中缺一交易日、同周两根等高 K 线、拆股双方可比与不可比、早收和时区。核对亮出的日周方案与单独运行 `analyze` 的同一子节点、K 线和规则。 [三角形可出现的位置](https://www.elliottwave.com/waveopedia/triangles/)

## WE-3 浪型细分与复杂调整

继续用既有 `Pattern` 大类，给 `PatternTemplate` 或候选增加可空的受限 `variant`。在 `engine/templates.py` 写父腿位置与许可子结构矩阵，在 `engine/rules.py` 计算端点证据，由 WE-1 验证内部。`check_pattern` 增加关键字 `variant`，默认旧行为。`engine/projector.py` 沿用条件价和目标区契约，新增亚型有自有条件时同步检查边界，目标依据不足时留空并说明。代码中登记每条规则的 ID、类型（硬规则、证据条件或评分指南）、适用阶段和来源。

约定 `q=s×P`，第一腿方向为正，端点比较沿用当前 epsilon。下列明确数值用于第一版工程识别，须记录版本和实测分母。

| 大类和新增亚型 | 实现与验收 |
| --- | --- |
| `FLAT` 的 `REGULAR_FLAT`、`EXPANDED_FLAT`、`RUNNING_FLAT` | 沿用 B 至少回撤 A 的 90% 的项目口径。规则平台要求 B 尚未越过起点且 C 越过 A 终点。扩散要求 B 越过起点且 C 越过 A 终点。顺势要求 B 越过起点且 C 落在 A 终点之前，B 超出幅度至少为 `max(tick, 0.10 × A长度)` 时才自动认定为该亚型。10% 是保守识别门槛。确认亚型要求 `3-3-5`。顺势分支移除旧的 `C_NEAR_A` 25% 限制，B 仅小幅越过且 C 未到 A 或 C 恰相等时保留通用平台待定。用 `[100,140,101,141]`、`[100,140,92,149]`、`[100,140,92,127]` 作端点正例，最后一例再配 B 内部五浪的反例。 [平台规则](https://www.elliottwave.com/waveopedia/flats/) |
| `TRIANGLE` 的 `BARRIER_TRIANGLE` | 在 A 浪方向归一后，突破侧对应 B–D，`abs(qD-qB) <= tick`，A–C 下降，观察区间内两轨分离并收敛。E 继续满足净回撤，且收敛侧规则要求 E 低于 C。沿用当前经典整组子浪准入，形态允许 E 穿越 A–C 延长线。位置通过 WE-2 或周期内父浪验证。等高近似只针对 B–D，A–C 平坦不触发该分支。验收 `[100,120,105,116,105,111]` 通过阻挡型端点，`[100,120,105,120,110,115]` 不可标为阻挡型，另测容差恰一 tick 和轨道交叉。 [三角形规则](https://www.elliottwave.com/waveopedia/triangles/) |
| `DIAGONAL` 的 `LEADING_DIAGONAL`、`ENDING_DIAGONAL` | 先实现收敛型。保留楔形独立的浪 4 重叠、非全回撤与浪 3 百分比规则。引导限于推动 1 或锯齿 A。终结限于推动 5 或允许 C 推动的位置，内部通过 `3-3-3-3-3`。引导的经典子浪口径有分歧，首版明确接受 `3-3-3-3-3`、`5-3-5-3-5` 两种细分。父腿未知时只显示通用楔形几何证据。用相同楔形端点分别放入父浪 1、3、5、A、C，核对位置和内部子浪组合。 [楔形规则及分歧](https://www.elliottwave.com/waveopedia/elliott-wave-pattern-diagonals/) |
| `IMPULSE` 的 `TRUNCATED_FIFTH` | 独立在已确认的 `q5 <= q3` 分支尝试，保留浪 2、3、4 及浪 5 百分比硬规则。第五腿必须按第 5 浪允许的驱动模板证明五条子浪齐备。普通完成态继续执行非截短 W5 进展规则。用 `[100,125,113,152,135,149]` 构造两条 K 线路径。W5 内部可证实为五浪的一条接纳，W5 内部仅 ABC 的一条排除。进行中 W5 尚未越过第 3 浪时只表示阶段待确认。 [截短规则](https://www.elliottwave.com/waveopedia/truncation/) |
| `WXY` 的 `DOUBLE_ZIGZAG`、`DOUBLE_THREE` | 双锯齿保留 W、Y 均为锯齿的类型，以及基线已有过滤口径。双重三浪的 W 允许锯齿或平台，X 为许可的调整结构，Y 允许锯齿、平台或三角形。W/Y 中锯齿至多一个、三角形只能放在 Y。内部结构和位置为必要证据。该分支使用通用的方向、影线和所选子结构规则，X 50% 与 Y 越过 W 属于旧双锯齿的过滤口径。验收平台–X–三角形、平台–X–锯齿、首段三角形和两个锯齿的错分类反例。 [双锯齿](https://www.elliottwave.com/waveopedia/zigzags/) · [组合调整](https://www.elliottwave.com/waveopedia/combinations/) |

各亚型先通过指定端点规则，再用同端点、不同内部走势的测试验证认证门槛。正反例均覆盖涨跌与价位边界。百分比长度以各浪实际起点手算，价格镜像后重新算预期。记录亚型的父级准入、结果阶段、报告显示和目标／失效位实现。

## WE-4 搜索、选窗、去重与性能

**实现位置**为 `engine/matcher.py`、`engine/span_selector.py`、`engine/subwaves.py`、`engine/scorer.py`、`service.py`。实现顺序是穷举参照、共享极值查询与预算、搜索调度，最后上线自动多窗及去重。

1. 在测试侧写独立穷举枚举器，输入至多 14 个确认拐点。按同一明确域枚举全部交替路径和合法跨度，覆盖最后确认点、进行中 tip 和指定的历史父腿终点，分开计算六类模板。可复用 `check_pattern` 来隔离搜索误差。另建人工给出预期的规则样本验证 `check_pattern` 本身。候选召回 = 搜索找到的参照候选数 / 合法参照数，空分母报告单列。精确锚点只枚举以指定交易日开始的候选。
2. 缓存冻结输入上 bar 区间的最高、最低查询，先用手算和逐根扫描验证与现有影线边界一致。单周期的各个窗口、父腿和子浪匹配共享一次 `SearchBudget`，日周关联用独立有界预算。每次尝试扩展扣 1，缓存命中可复用结果，记录按窗口和尺度的扩展、剪枝、缓存命中以及截断原因。缓存键含输入／截止、价格坐标、端点、尺度、模板／亚型、规则版本和深度。截断的区间结果只报告已取得证据。
3. 在尺度、锚点和方向间稳定轮转，优先给不同可行分支各一次初始搜索，再按已验证的结构证据与路径成本扩展。事实性硬规则才可提前剪枝。得分排序、锚点远近和跳过点数只影响调度和待展示顺序，诊断分别标记 beam 宽度和总预算限制。给定同一输入和配置，迭代次序、结果与截断诊断须可复现。
4. 自动窗口沿用 `select_anchor_details` 从已确认宏观高低中取点。日用最多比较三套搜索窗口。主窗用正常自动范围，备选一取最近且与主候选起点极性相反的合格宏观锚点，备选二取能改变实际范围的最早合格锚点。实际范围相同的窗口去重。已无第二合格窗是 `NOT_APPLICABLE`，数据或预算不充分是 `INCONCLUSIVE`。同主偏向但关键端点不同是结构差异，主偏向反转展示 `CHANGED_BIAS`，同时显示参与窗口、具体分歧和证据覆盖。用户显式锚点维持精确匹配。周线背景和备选窗共用既有请求截止，复用已装载行情。
5. 候选按 `(timeframe, degree, pattern_type, variant, direction, stage)` 分桶。已确认端点用原始 bar ID 去重，多个尺度、锚点只增加证据来源。允许近似合并的初始口径为所有对应端点相差至多 1 根本周期完整 K 线、价差至多一个 tick、最后确认标签一致，且失效条件的 `kind/side/op` 相同、边界价差至多一个 tick。用按证据排序的代表做直接比较，避免传递合并吞掉不同结构。每组以最佳成员评分入权重计算，原成员证据仍可查询。`AnalysisContext.qualified`、报告和高级模式结算使用实际代表 ID，权重总和与省略权重可核对。保留原模式的排序、权重与 `qualified` 输出。

**通过条件**。预算充足时，小样本合法候选集合与穷举完全相同。紧预算记录截断，保持可复现，保留每个已尝试来源。注入重复尺度和锚点时，同一结构权重不会膨胀。换窗分歧和两套同向但不同父浪的结构均能看见。固定大样本对比 `0.2.1` 与高级模式的候选召回、截断率、主选换窗稳定性和耗时，交付前冻结性能口径。

## WE-5 主备演进、条件触发与逐根回放

**实现位置**为 `brief.py`、`schemas/brief.py`、`history.py`、`evaluation/decisions.py`、`evaluation/outcomes.py` 和报告渲染器。提取 `compare_decisions(previous, current, previous_source, current_source) -> DecisionComparison`，供 `attach_previous` 和评估脚本共用，保持 `BriefPrevious` 原字段兼容。复用 `_source_revision`、`provider_price_scale`、`outcome_window`、`condition_hit`。

1. 判断顺序是标的／feed／周期／价格口径可比性，旧截止前的行情和因子变动，规则与配置变化，最后才比较候选。记录独立的 `input_change` 和 `structure_change`，允许同时发生配置升级与数据修订。修订计数限定在旧决策可见范围。只有新 K 线的情形维持相同口径，旧快照无法验证时明确记为不可比。
2. 对照已确认端点前缀、父子关系、上次主备名单和本次候选。演进原因至少包括 `TIP_MOVED`、`STAGE_ADVANCED`、`HARD_INVALIDATED`、`ALTERNATE_PROMOTED`、`EVIDENCE_CHANGED`、`SEARCH_LIMITED`、`COUNT_CHANGED`。区分已确认端点新增与已有端点改变。一次比较可记录多个事实，例如旧主选先触发硬失效、旧备选随后成为主选。证据给出旧新候选 ID、最先分歧的浪标签、规则 ID 和首个触发 bar。
3. 用上次发布的条件价、`active_from` 和坐标，将之后已完成的日／周 bar 重放到本次截止。保留 `CONFIRMATION`、`STAGE_EXIT`、`INVALIDATION` 原类别，逐条给出触发会话、价格侧、实际观测和 OHLC 来源。涉及目标区或确认与失效先后时，复用 `first_touch` 的 `AMBIGUOUS_SAME_BAR` 口径与跳空规则。跨拆股用现有转换验证，失败记 `PRICE_BASIS_UNVERIFIED`。有预期交易缺口时标明证据不完整，缺口之后的触达只报告已观测日期。
4. `scripts/evaluate_briefs.py` 读取冻结协议，用正式 `build_brief` 与同一比较函数产生逐前缀决策。`decisions` 记录结构键、首选切换及原因、阶段变动、条件触发、父子验证、搜索限制。`summary` 按市场、标的类型、周期和浪型记录可比较分母、拐点改写、极值至确认延迟、可解释切换率及缺数据。保持 `FROZEN_DATA_REPLAY` 和发布报告身份。历史库仍追加新报告和证据，原报告保留原时点的认识。

**通过条件**。对同一版本的行情，用完整快照加 `as_of` 与物理切断后的前缀走真实 CLI，在相同价格基准下核对已确认拐点、父子证据、主备、价位和变更原因，忽略物理来源 ID 与运行元数据。压测确认前一根／当根／后一根、备选晋升、新行情只是移动 tip、单根同时触发目标和失效、修订在旧截止之后、配置变化和拆股无法转换。`tests/test_brief.py`、`tests/test_replay_and_reports.py`、`tests/test_settlement.py` 验证 `brief`、`watchlist`、`history` 与事件回放的输出一致。 [已有决策回放契约](WAVE_ANALYSIS_DESIGN.md)

## DeepSeek 交付物与 Codex 验收

### 实施交付

1. 按 WE-1 至 WE-5 给出提交或 diff 对照表。每条规则更改列实现位置、样例路径、预期结果、报告字段和版本。每项先交可运行的入口和正反例，再交性能优化。WE-1、WE-2 与 WE-5 优先通过普通推动和锯齿，随后接入 WE-3 的亚型。WE-4 的穷举参照从第一批开始使用。
2. 提交 `docs/protocols/wave-engine-acceptance-v1.json`，冻结标的、快照 ID、样本时间网格、分组、耗时阈值和合成样本 ID。沿用 `scripts/evaluate_briefs.py` 的 `settings` 与 `samples` 契约，结构验收设 `events: []`。每组样本以 `source_timeframe` 指明日或周，从同一份日线源聚合。沪深个股与指数、美股个股与 ETF 各选至少一个可读快照。优先复用 `600519.SH`、`000300.SH`、`AAPL`、`SPY` 已验收快照，再加入停牌、拆股和未完成周的合成反例。基线和高级模式分别写协议和产物，两个协议的样本及截止网格须相同。参数和门槛在对比前登记。
3. 提交高级模式配置示例和更新后的中文使用手册，写明何时标为证据不足，如何展开浪树、读主备差异和重放条件。交付一份验收记录，附源码和配置指纹、可复现命令、样本与失败明细。生成 HTML、行情及运行产物放在忽略目录。

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
git diff --check
.venv/bin/python scripts/evaluate_briefs.py --protocol docs/protocols/wave-engine-acceptance-v1.json --output-dir reports/wave-engine/acceptance
.venv/bin/eliot brief AAPL --offline --snapshot "$AAPL_SNAPSHOT" --config "$ADVANCED_CONFIG" --json
.venv/bin/eliot analyze 000300.SH --timeframe 1w --offline --snapshot "$CN_INDEX_SNAPSHOT" --config "$ADVANCED_CONFIG" --json
```

### 独立验收门槛

| 验收项 | Codex 核验和通过条件 |
| --- | --- |
| 规则真值 | 手工构造独立 K 线和拐点，重算硬条件、每腿结构、允许位置与失效价。强制正例、反例、边界和阶段状态全部通过。每次新增规则需能在正式入口和报告中追溯。 |
| 数据因果与版本 | 对冻结样本逐前缀复算。相同输入版本的已确认拐点改写数为 0，未来 bar 参与计算数为 0。已有 `0.2.1` 报告在新代码中可校验，并可按原条件核销。旧模式业务载荷与基线一致，高级结果身份和报告渲染版本可区分。 |
| 搜索与展示 | 小规模充足预算的候选集合与独立穷举相同。预算不足输出可定位。核对日周匹配、结构聚合、主备切换解释、权重及所有省略项。真实样本允许多解和待定，人工复核引擎给出的证据与可比性。 |
| 日用性能 | 同一机器、同一依赖、同一冻结输入，每个市场至少 30 个可分析时点，预热后各跑至少 3 轮，分别记录纯分析与跨周期摘要的 p50/p95。拟以高级模式 `brief` p95 不超过 `0.2.1` 基线的 2 倍作为默认切换门槛，同时公布截断率和结构验证覆盖。达不到门槛可以保留显式高级入口，继续针对热点优化。 |
| 可安装与可使用 | 构建 wheel 并在临时安装目录验证版本、依赖与包内代码，实跑沪深和美股的离线 `analyze` 与 `brief`。核对 JSON、终端及 HTML 所示规则、价格坐标、模式和时间。 |

Codex 对每个编号记录通过、需修复或证据不足。严重硬规则错误、前视、错误价格坐标、旧报告无法校验仍须修复后再进入日用默认版。概率实验按原协议单独评审，波浪引擎以结构正确性和可复核性验收。
