# C–G 首版验收记录

日期 2026-11-19。范围：历史查询、自选批量、离线评估、概率训练/推理、盲标评分。A、B 见前两份记录。

源码指纹 `0cfe453c5d02a245ffca148d76656ec337aa414551cf08b94350ece61d554342`；本次完整测试 254 项通过。

## C 历史查询与上次判断

- 新增 `HistoryStore`：SQLite `briefs` 表，保存 brief digest、`lineage_key`、`base_lineage_key`、行情截止、发布时间、源快照、报告路径和模式。
- 发布 `brief` 后先校验 manifest/文件哈希，再登记索引。
- `elliottwaver history SYMBOL` 支持 `--as-of`、`--mode live|frozen`、`--json`。
- `brief.previous` 支持 `FIRST_RECORD / SAME_COUNT / ALTERNATE_PROMOTED / STAGE_ADVANCED / COUNT_CHANGED / CONFIG_CHANGED / DATA_REVISED / NOT_COMPARABLE`。
- 配置变化：比较上一份与当前摘要的 `config_hash`、`engine_version`；变化时输出 `CONFIG_CHANGED`，并保留结构变化和修订计数。
- 数据口径变化：比较 `feed`、`adjustment`、`timeframe` 和证券身份；变化时输出 `NOT_COMPARABLE`，原因列出具体字段。
- 数据修订检测：比较重叠原始 K 线 OHLC/量额，以及按生效日期对齐的因子值；修订时输出 `DATA_REVISED`，源不可读/无重叠时输出 `NOT_COMPARABLE`，新增 K 线不算修订。
- `base_lineage_key` 以证券为稳定身份，配置、feed、复权或周期变化后仍能找到上一份摘要并给出 `CONFIG_CHANGED` / `NOT_COMPARABLE`；`lineage_key` 保留完整身份。旧历史库在打开时自动迁移并回填 `base_lineage_key`。
- `BriefPrevious` 增加 `structure_change / data_revision / revision_count / factor_revision_count`，保留结构变化和修订计数。

真实快照烟测（茅台 `2026-02-27` → `2026-03-31`）：

```text
2026-02-27  FIRST_RECORD，无候选
2026-03-31  COUNT_CHANGED，上次没有候选，本次出现候选
```

修订检测烟测：

```text
同一源：        SAME_COUNT / NONE / 0 / 0
修订 OHLC：     DATA_REVISED / REVISED / 1 / 0 / structure_change=SAME_COUNT
仅新增 K 线：   SAME_COUNT / NONE / 0 / 0
修订因子：      DATA_REVISED / REVISED / 0 / 1
```

配置变化烟测：

```text
config_hash 变化：    CONFIG_CHANGED / NONE / structure_change=SAME_COUNT
engine_version 变化： CONFIG_CHANGED
feed 变化：           NOT_COMPARABLE（原因含 feed）
timeframe 变化：      NOT_COMPARABLE（原因含 timeframe）
历史库：config-a 与 config-b 的 lineage_key 不同，base_lineage_key 相同；
        previous 仍找到 config-a 的记录。
CLI：2026-03-27 默认配置 → 2026-03-30 + --subwave-depth 1，
     previous.change = CONFIG_CHANGED，data_revision = NONE，
     reason = 引擎或配置变化：config_hash；结构：主选结构发生变化。
周线 1w 接日线历史：
     previous.change = NOT_COMPARABLE，reason = 数据口径不同：timeframe。
```

## G 自选批量

```bash
printf '600519.SH\n000300.SH\n600519.SH\n' > watchlist.txt
.venv/bin/elliottwaver watchlist watchlist.txt --output-dir reports/watchlist \
  --workers 2 --retries 1 --retry-delay 0 \
  --model-registry reports/model-registry/registry --json
```

- `--retries 0..5`：每个标的有限重试；网络/源错误可重试，数据不足的 `SystemExit(3)` 不重试。
- `--workers 1/2/4/8`：有界线程池并发；结果保持输入顺序。
- 每个记录包含 `attempts`、状态、摘要、路径和错误。
- 单个标的错误会记录 `ERROR` 并继续其他标的；全部处理完后返回首个失败退出码。

模型注册表：

```python
registry = ModelRegistry(Path("reports/models"))
registry.register(Path("reports/models/model-v1.json"))
registry.register(Path("reports/models/model-v2.json"))
registry.withdraw(model_v2_id)   # 自动回退到 v1
registry.load()                  # 返回 v1；全部撤回则返回 None
```

`brief --model-registry DIR` 与 `watchlist --model-registry DIR` 使用同一注册表；当前模型缺失/损坏/撤回时回退 `previous`，没有可用模型时保持 `UNCALIBRATED`。

烟测结果：

```text
2 条记录（重复的 600519.SH 被去重）
600519.SH  OK，attempts=1
000300.SH  OK，attempts=1
注册表 current -> second
撤回 second -> fallback first
撤回 first -> None
```

### G 增量核销

新增 `elliottwaver/settlement.py`、`elliottwaver settle` 命令和 `elliottwaver support-matrix` 命令。

- `SettlementStore` 使用 SQLite：
  - `settlements` 以 `(brief_digest, event_id, scope)` 为唯一键；`scope` 为候选 `scenario_id` 或标的（收盘区间事件）。
  - `settlement_revisions` 保存不可变修订历史，含 `revision`、`status`、`outcome`、`session`、`value`、`settlement_snapshot_id`、`settlement_input_snapshot_id`、`price_coordinate_id`、`source_hash`、`is_initial`、`supersedes_revision`。
- `register_brief` 只为未登记的键插入 `PENDING` 记录；重复调用幂等。
- `settle_brief` 只推进未成熟记录：
  - 首次结算写入 revision 1（`is_initial=1`）。
  - `RIGHT_CENSORED` / `INCOMPLETE_DATA` 在更多行情到达后推进到 `SETTLED`，生成 revision 2。
  - 已成熟记录不会被非成熟观测回退。
  - 行情修订导致 `source_hash` 或结果变化时新增 revision，`supersedes_revision` 指向上一版；最初观察保持不变。
  - 结算绑定 `settlement_snapshot_id`、输入快照、价格坐标和未来行情哈希。
- 模型 `model_id` 和发行时概率写入结算记录，结算或模型撤回不改写。
- 支持矩阵按资产类型 × 周期 × 事件输出 `model_status`、`applicable_model`、测试状态和未通过原因；模型状态来自 E 的 `summary.json`，没有批准模型时为 `UNCALIBRATED`。

真实烟测（茅台已发布 brief）：

```text
首次结算：
2026-02-27 CLOSE_BAND_5D_V1  SETTLED  rev 1  DOWN  snapshot 8378a32d
2026-02-27 CLOSE_BAND_20D_V1 SETTLED  rev 1  DOWN  snapshot 8378a32d
2026-03-31 CLOSE_BAND_5D/20D RIGHT_CENSORED rev 1
2026-03-31 CONFIRM/TARGET     RIGHT_CENSORED rev 1

修订未来第 5 根 K 线后：
CLOSE_BAND_5D_V1  rev 2  initial DOWN(8378a32d) -> latest UP(a5a1a600)，supersedes=1
CLOSE_BAND_20D_V1 rev 2  initial DOWN(8378a32d) -> latest DOWN(a5a1a600)，supersedes=1
```

支持矩阵烟测（E 模型 `cbe2c3e6...`）：

```text
rows 16
EQUITY 1d CLOSE_BAND_5D_V1  INSUFFICIENT_EVIDENCE  applicable_model=false
  原因含 frequency/momentum/price 基线未通过
INDEX  1w CLOSE_BAND_20W_V1 UNCALIBRATED（无模型摘要时）
```

支持矩阵证据：[`2026-11-19-g-support-matrix.json`](2026-11-19-g-support-matrix.json)。

## D 离线评估

新增 `scripts/evaluate_briefs.py` 与 `evaluation/decisions.py`、`evaluation/outcomes.py`。

支持事件：

- 日线：`CLOSE_BAND_5D_V1`、`CLOSE_BAND_20D_V1`、`CONFIRM_FIRST_20D_V1`、`TARGET_FIRST_20D_V1`
- 周线：`CLOSE_BAND_5W_V1`、`CLOSE_BAND_20W_V1`、`CONFIRM_FIRST_8W_V1`、`TARGET_FIRST_8W_V1`

日线茅台 2025-01-01 至 2025-06-30、周度决策烟测：

```text
decisions 27，analyzable 27
CLOSE_BAND_5D:  UP 3 / RANGE 17 / DOWN 7
CLOSE_BAND_20D: UP 12 / RANGE 6 / DOWN 9
CONFIRM_FIRST:  CONFIRMED 3 / INVALIDATED 1 / EXPIRED 1 / UNDEFINED 3
TARGET_FIRST:   TARGET 2 / INVALIDATED 2 / EXPIRED 2 / UNDEFINED 2
```

周线茅台 2023-01-01 至 2025-06-30 烟测：

```text
decisions 127，analyzable 127
CLOSE_BAND_5W / 20W：各 127 行
CONFIRM_FIRST_8W：39 行，CONFIRMED 5，可判定 25，条件命中率 20%
TARGET_FIRST_8W：39 行，TARGET 12，可判定 39，条件命中率 30.8%
```

`summary.json` 增加 `by_symbol`、`by_asset_type`、`by_timeframe`、`by_pattern`、`by_horizon` 分解；每个决策分解保留 decisions、analyzable、排除原因、结构指标和事件统计。输出 `decisions.jsonl`、`events.parquet`、`summary.json`、`manifest.json`。

### D 结构指标

`decisions.jsonl` 每个决策行新增：

- `first_pattern`、`first_stage`：首选结构模板和阶段。
- `search_truncated`：本次搜索是否被截断。
- `new_pivots`：相对同一标的上一个决策新确认的拐点；首行包含当时全部已确认拐点。每个拐点含稳定 `pivot_key`（基于原始 bar ID，避免复权基准变化导致误判）、`pivot_id`、尺度、状态、极值日、确认时间、`bars_to_confirm`、调整后价格和原始价格。
- `confirmed_pivot_count`：当前决策的已确认拐点总数（含此前已确认）。
- `pivot_rewrite_count`、`pivot_new_count`：相对同一标的上一个决策的已确认拐点改写数和新确认数。
- `primary_switch`：相邻两个可分析且都有首选的决策，首选结构键是否切换。

`summary.json` 的 `structure` 包含：

- `confirmed_pivots`（唯一拐点数）、`confirmed_pivot_observations`（跨决策观测数）。
- `pivot_rewrite_pairs`、`pivot_rewrites`、`pivot_rewrites_per_pair`、`pivot_rewrite_rate`（至少发生一次改写的相邻对占比）、`new_pivots`。
- `confirmation_delay`：极值至确认 K 线数的 count / mean / median / p90 / max，并按尺度分解。
- `signals`、`prediction_coverage`（有首选的决策 / 可分析决策）、`no_signal`。
- `primary_switches`、`comparable_primary_pairs`、`primary_switch_rate`。
- `search_truncated`。

每个事件的统计另含 `coverage`（事件行 / 可分析决策）和 `status_rates`（各结果状态占比）；`success_per_100_decisions` 与 `conditional_hit_rate` 继续保留，零分母为 null。

真实日线烟测（茅台 2023-01-01 至 2025-12-31，154 个周度决策）：

```text
confirmed_pivots            605
confirmed_pivot_observations 80820
pivot_rewrite_pairs         153
pivot_rewrites              0
new_pivots                  161
confirmation_delay          count 605 / mean 5.11 / median 3 / p90 11 / max 67
prediction_coverage         0.461（signals 71 / no_signal 83）
primary_switches            31 / 53 comparable pairs = 0.585
search_truncated            0
按尺度确认延迟              1.4: mean 2.74 / median 2 / max 15
                            2.4: mean 6.82 / median 5 / max 34
                            3.8: mean 15.72 / median 11 / max 67
```

该样本中原始 ZigZag 的已确认拐点稳定，改写数为 0；`pivot_new_count` 记录新增确认。单元测试另外覆盖“拐点被移除”和“原始价格被修订”两种情况，确保改写计数能检测数据修订。

## E 概率训练与推理

新增 `scripts/train_forecasts.py`、`evaluation/features.py`、`evaluation/calibration.py`、`forecast.py`。

- 特征：20 日收益、ATR/价格、候选数量、方向权重、首选质量/覆盖、前二质量差、候选模板/阶段、确认/失效距离。
- 训练：NumPy 多项逻辑回归、训练/校准/留出切分、温度校准、Brier/log loss、可靠性分档。
- 三项对照：训练集类别频率、仅 20 根动量、完整价格组（动量 + ATR + 缺失标记，候选事件另加确认/失效距离）；动量和价格组使用同样的温度校准。
- 发布门槛：相对三项基线，Brier 和 log loss 改善的配对 95% 区间下界均大于 0；每个展示可靠性档至少 50 个成熟观测、10 个不同决策日期，预测均值减实际发生率的 95% 区间在 ±0.10 内；测试至少 50 行、10 个日期、5 个标的；支持档覆盖率至少 0.5。
- 输出 `model.json`、`summary.json`、`manifest.json`；`model.json` 记录事件版本、适用范围、分割、引擎/配置摘要、特征顺序、编码、参数、类别顺序、温度、支持档、样本量和验证摘要。
- `brief --model model.json` 只加载 `APPROVED` 事件；未获支持显示 `INSUFFICIENT_EVIDENCE` 或 `UNCALIBRATED`。线上三分类任一分量落入未获支持档位时整组置空，显示 `UNSUPPORTED_BIN`。
- `ModelRegistry` 支持 current/previous、撤回、损坏/缺失和引擎/配置/周期/品种/feed/复权兼容性回退；`brief` 默认读取 `.cache/elliottwaver/models`，`--model-registry` 与 `watchlist --model-registry` 共用。
- 当前扩大样本审批结果见 [E 模型审批记录](2026-11-19-e-model-approval.md)。

## F 盲标评分

新增 `scripts/label_wave_cases.py`：

```bash
# 从已发布 brief/daily 报告自动生成分层盲标 case
.venv/bin/python scripts/label_wave_cases.py export \
  --briefs reports/CN/600519.SH/brief --cache-root .cache/elliottwaver \
  --bar-window 60 --output blind-cases.json --html blind-cases.html

# 通用 case 输入校验
.venv/bin/python scripts/label_wave_cases.py export --input cases.json --output blind.json

# 标签导入与评分（可选 cases 文件用于 split 和缺交统计）
.venv/bin/python scripts/label_wave_cases.py import --input labels.csv --output labels.json
.venv/bin/python scripts/label_wave_cases.py score \
  --labels labels.json --cases blind-cases.json --output score.json
```

自动导出会：

- 扫描 `brief.json` / `daily.json`；
- 从源快照加载截至决策日的 K 线（日线或派生周线）；
- 为每个候选生成 `case_id`、证券、周期、决策日、资产类型、浪级、方向、端点、K 线和分层 `stratum`；
- 对无候选 brief 生成 `NO_CANDIDATE` case（空端点、`HIGH` 歧义、`NO_QUALIFIED_CANDIDATE`/状态警告）；
- 解析 `EXCLUDED_STRUCTURE`/拒绝候选生成 `EXCLUDED_STRUCTURE` case；端点日期不完整时标记 `ENDPOINT_DATES_PARTIAL`；
- 为每个 case 生成 `ambiguity = LOW / MEDIUM / HIGH`，`stratum = 资产类型|浪级|方向|歧义`；
- 隐藏 `pattern_type`、`stage`、`name`、`role`、`scenario_id`、质量/权重、拒绝原因等模型字段；
- 按 `case_id` 去重，并支持 `--max-per-stratum`；
- 源快照不可用时仍导出端点，但标记 `BARS_UNAVAILABLE`；
- 输出 `case_counts` 汇总 `CANDIDATE / EXCLUDED_STRUCTURE / NO_CANDIDATE`。

真实烟测（茅台 `2026-03-31` brief）：

```text
cases 3
case_counts {'CANDIDATE': 1, 'EXCLUDED_STRUCTURE': 2}
CANDIDATE b07b459ed61cd1972ab8a2ac MINOR DOWN HIGH
  EQUITY|MINOR|DOWN|HIGH, bars 60, endpoints 3
EXCLUDED_STRUCTURE ab182df18dd511a541e9faf0 UNKNOWN UNKNOWN HIGH
  EQUITY|UNKNOWN|UNKNOWN|HIGH, endpoints 4, ENDPOINT_DATES_PARTIAL
EXCLUDED_STRUCTURE de9243dd8e20d54f33e4069b UNKNOWN UNKNOWN HIGH
  EQUITY|UNKNOWN|UNKNOWN|HIGH, endpoints 4, ENDPOINT_DATES_PARTIAL
```

无候选真实烟测（茅台 `2026-02-27` brief）：

```text
cases 3
case_counts {'NO_CANDIDATE': 1, 'EXCLUDED_STRUCTURE': 2}
NO_CANDIDATE b78984e6e1e6b1c1b8fb26c2 UNKNOWN UNKNOWN HIGH
  EQUITY|UNKNOWN|UNKNOWN|HIGH, endpoints 0, bars 60
```

`--html` 生成自包含静态盲标页面：K 线缩略图、端点、模板多选、弃权、理由、标注者 ID 和 CSV 下载；页面显示类型/歧义/分层，不包含 `pattern_type`、`name`、`scenario_id`、权重、拒绝原因或未来行情。3-case 烟测页面 8,497 字节，包含 SVG 和模板多选，不含模型字段。

对上述 3 个真实 case 重新评分（2 名标注者）：

```text
cases 3, labeled 3, missing 0
IMPULSE:  recognition 0.500, double 3, dp 1, dn 1, kappa 0.400,
          gate INSUFFICIENT_EVIDENCE（样本不足）
DIAGONAL: recognition 0.500, double 3, dp 1, dn 1, kappa 0.400,
          gate INSUFFICIENT_EVIDENCE（样本不足）
```

50/10/10 门槛烟测仍通过：`train` 50 个双人 case、dp 10、dn 40、κ 1.000 → `PASS`；`test` 5 个 case → `INSUFFICIENT_EVIDENCE`；缺交 1。

### 独立判读认可概率与门槛

`score` 现在输出每模板：

- `label = INDEPENDENT_RECOGNITION` 和 `blind_protocol_version`。
- `recognition_probability`：按 case 均权的独立判读认可概率。每个 case 先对其全部有效（非弃权）判读取均值，再对所有有效 case 等权平均；同一 case 的多份判读不会被拆散或按判读数加权。
- `recognition_cases`、`cases`（双人有效 case 数）。
- `double_valid_cases`：恰好两份有效判读的 case 数。
- `double_positive`、`double_negative`、`discordant_count`、`discordant_cases`。
- `positive_rate_left/right`、`annotation_positive_rate`、`case_positive_rate`。
- `raw_agreement`、`cohen_kappa`。
- `gate`：每模板至少 50 个双人有效 case、至少 10 个双阳性、10 个双阴性、Cohen κ ≥ 0.60。未通过时 `eligible_for_calibration=false`，状态为 `INSUFFICIENT_EVIDENCE` 或 `LOW_AGREEMENT`。即使某模板没有正例，也会输出 0 计数和对应门槛原因。

`score` 顶层另输出：

- `by_split`：按 `split` 分别计算同一组指标和门槛；同一 case 的全部判读保留在同一 split。
- `missing_cases`、`missing_case_ids`：cases 文件中没有标签的 case。
- `abstain`、`abstain_count`、`empty_selection`、`cases_without_valid`、`cases_with_extra_annotations`。

CLI 烟测（50 个 train 双人 case + 5 个 test 双人 case + 1 个缺交）：

```text
train: recognition 0.200，double 50，dp 10，dn 40，kappa 1.000，gate PASS
test:  recognition 1.000，double 5，dp 5，dn 0，kappa null，gate INSUFFICIENT_EVIDENCE
missing_cases 1（missing-case）
overall: recognition 0.273，double 55，dp 15，dn 40，kappa 1.000
```

这里的“独立判读认可概率”是标签统计目标，不是已校准的模型输出；类别概率仍需按 E 的留出误差检查单独验证。当前是静态 HTML 盲标页面，不是完整多用户 Web 应用。

### 标的涨跌与每模板二项模型

- 盲标页面新增标注者涨跌单选：`UP / DOWN / ABSTAIN`；`import` 保留 `direction`；`score` 输出 `directions` 的方向计数、双人一致率和 Cohen κ。
- 新增 `label_wave_cases.py direction`：从 case 的 `source_snapshot_id` 和 `decision_date` 读取未来行情，输出实际 `UP / DOWN / FLAT`、`RIGHT_CENSORED`、`decision_close`、`future_close`、`return`；该文件不进入盲标 case。
- 新增 `src/elliottwaver/label_models.py`、`scripts/train_label_models.py` 和 `label_wave_cases.py predict`：
  - 每模板独立二项逻辑回归。
  - 目标为该 case 全部有效判读中认可该模板的比例，按 case 均权。
  - 同一 case 的判读必须留在同一 split；跨 split 直接报错。
  - 训练/校准/留出、温度校准、频率与浪级基线、Brier/log loss 配对区间、可靠性分档和 E 式发布门槛。
  - 只有 `APPROVED` 模板进入 `predict`。

合成 300-case 烟测（train 50 / calibration 50 / test 200）：

```text
IMPULSE status APPROVED
label_gate PASS（50 double / 10 dp / 40 dn / kappa 1.0）
test 200 cases / 20 dates / 20 symbols
supported_coverage 1.0
model Brier ~0.0000000004
frequency baseline 0.1900
degree baseline 0.1900
```

真实人工标签尚未达到 50/10/10/κ 门槛，因此没有真实批准标签模型；详见 [F 标签模型记录](2026-11-19-f-label-models.md)。

## 回归

```text
254 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
```

## 边界

- C 的修订检测只覆盖重叠原始 K 线和共同因子日期；源不可读、无重叠或一侧缺因子时标记 `NOT_COMPARABLE`。
- D 的周线网格从日线快照派生或直接使用周线快照；周线事件期限为 5/20 周和 8 周结构事件。
- E 的审批门槛仍是研究初值，不是设计中的完整置信区间/可靠性发布门槛。
- F 已支持从报告自动生成盲标 case、静态 HTML 盲标页面和 CLI 评分；完整多用户 Web 界面仍未实现。
- G 已支持有界并发、有限重试和模型注册表回退；更大规模并发和真实网络抖动仍未压测。
- 所有改动未提交，等待验收。
