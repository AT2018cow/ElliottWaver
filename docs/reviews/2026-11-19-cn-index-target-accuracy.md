# 指数 20 日目标先达：滚动校准、目标几何与失效审计验收

> 预测明细与规则审计已重新生成。2022 年历史压力测试和 2026 年留出重算见[11 月 20 日复验](2026-11-20-cn-index-probability-recheck.md)。

本轮按[实施与验收计划](../protocols/cn-index-target-accuracy-work-plan.md)完成 `TARGET_FIRST_20D_V1` 的开发期三项工作：滚动校准、目标区几何特征和缺失效边界审计，并跑完 A–D 四组对照。范围固定为 7 个沪深京指数、`sina-index` 无复权日线和每周最后一个交易日。选择只使用 2023–2025 开发期；2026 留出未参与本轮选型。

## 结论

- E1 滚动校准已实现为纯函数，只用 `label_available_at <= decision.as_of` 的明确结果；样本不足回退 `delta = 0`，同一决策日的全部候选共用同一状态。因果回归覆盖未来标签改写、到期 bar 未可用、缺到期 bar、同根双触及（`AMBIGUOUS_SAME_BAR`）排除和同日候选共享状态。
- E2 目标几何已进入 V3 目标先达模型和同一事件的价格基线。V2 评估与旧冻结候选仍可读；V3 缺新列时评分报错。
- E3 复算得到 3,138 个目标事件、1,548 个未定义、1,534 个缺失效、14 个有失效但缺目标、1,590 个同时具备目标与失效。1,380 个缺失效属于只可给 `STAGE_EXIT` 的结构，154 个属于条件性硬失效未触发；未发现规则应给 `INVALIDATION` 但实现漏发的样本。
- A–D 四组合并结果中，C（V3 + 固定校准）按 log loss 入选：Brier 0.0917、log loss 0.3207，优于 A 的 0.1057／0.3487。但 C 相对含目标几何的价格基线为负增益（Brier −0.0056，log loss −0.0189，95% 区间均低于零），D 同样为负。**波浪结构在获得同样目标几何后没有建立增量；收益主要来自目标几何。**
- 没有新增独立前向留出，候选状态为 `RESEARCH_COMPLETE_AWAITING_HOLDOUT`，不注册正式模型，也不把开发期选择写成发布证据。

## 实验样本与对齐

V3 评估用 7 个指数原快照重放 2023-01-01 至 2025-12-31，共 1,078 条决策、11,653 条目标先达事件。按预定到期日不晚于 2025-12-31 过滤后，V2/V3 各有 11,239 条全部事件，其中 5,615 条可判定（972 正、4,643 负）。

`decision_id` 的摘要包含协议身份，V2/V3 的协议 ID 不同，因此 5,615 条决策 ID 全部不同。跨版本对齐改用 E2 要求的稳定键 `(symbol, decision_date, candidate_key)`，并逐项核对候选集合、标签、决策日期、预定到期日、快照 ID 和状态；未发现不一致。逐决策概率文件保留各自版本的 `decision_id` 和稳定对齐键。

## E1 滚动校准

实现位于 `src/eliotbot/evaluation/calibration.py` 的 `rolling_logit_offset()`：

- 只接收 `label_available_at <= decision.as_of` 的明确结果；早触价仍等到预定到期 bar 可用。
- 取最近 12 个不同决策日，每个日期保留全部可用标的和候选；至少 8 日、100 条、正负各 10 条才估计。
- 目标函数为平均二元交叉熵加 `0.1 * delta**2 / 2`，在 `[-2, 2]` 内二分求解。
- 不足门槛时 `delta = 0`，并记录 `dates/rows/positive/negative/last_label_available_at/window_start/window_end/reason`。
- 同一决策日只生成一个状态，所有候选共用；基础权重和静态校准在每个验证折开始前冻结。
- V3 评估已把 `label_available_at` 写入全部 11,653 条目标事件；V2 旧事件没有该字段，对照脚本从源快照的到期 bar `usable_at` 推导。

专项测试在 `tests/test_index_target_accuracy.py`，覆盖：到期前不进入窗口、缺到期 bar 排除、未来标签改写不影响更早状态、同日候选共享状态、确定性与回退。开发期 B/D 的逐折回退日期均为 0/22，因为校准段已积累足够结果。

## E2 目标区几何

V3 在目标先达事件特征末尾增加：

```text
target_distance_atr
inside_target_zone
missing_target_geometry
```

计算使用 `scenario.target_zones[0]` 和决策时价格坐标：`max(L-C, C-H, 0)/ATR`；区间内为 1；缺目标、区间非法或 ATR 不可用时距离和区间内均为 0、缺失标记为 1。价格基线 `_price_order(EVENT, "3")` 同步加入同样三列，确保波浪模型与价格基线使用相同目标信息。

协议字段 `feature_version` 选择列集：缺省为 2，V3 为 3；评估清单、模型特征顺序和候选哈希同步校验。`feature_vector()` 在 V3 列缺失时抛出 `Missing target geometry feature`，避免静默补零。测试覆盖 `[100,104]`／ATR=2 的距离与边界、多个目标区取首个、无目标、无效 ATR、涨跌镜像和 V3 缺列报错。

## E3 失效边界审计

审计入口 `scripts/audit_index_target.py`，产物在 `reports/index-target-accuracy/audit-v2/`。对 V2 评估的 2026 留出复算：

| 项目 | 数量 |
| --- | ---: |
| 目标事件（预定到期日 ≤ 2026-11-16） | 3,138 |
| 未定义 | 1,548 |
| 缺失效 | 1,534 |
| 有失效但缺目标 | 14 |
| 同时具备目标与失效 | 1,590（50.7%） |

三项对账均成立：`1,548 = 1,534 + 14`；缺失效等于阶段×模板计数之和；`3,138 = 1,548 + 1,590`。

| 规则分类 | 数量 | 占缺失效 |
| --- | ---: | ---: |
| 只可给出阶段退出线（`STAGE_EXIT_ONLY`） | 1,380 | 90.0% |
| 条件性硬失效本轮未触发 | 154 | 10.0% |
| 规则应给失效但可能漏发 | 0 | 0.0% |
| 特征缺失或证据不足 | 0 | 0.0% |

阶段×模板计数与代表案例、条件类型和 SVG 图见[审计报告](../../reports/index-target-accuracy/audit-v2/audit.md)。代表案例覆盖 `COMPLETE`（推动、三角形）、`B_DEVELOPING`（平台）、`C_DEVELOPING`（三角形）、`W5_DEVELOPING`（推动）以及一个有失效缺目标的对角线候选。`STAGE_EXIT` 仍用于阶段结束；12 条低样本行被标记 `LOW_SAMPLE`，但规则分支本身明确。

## A–D 四组对照

三折与计划一致：训练从 2023-01-01 开始，后两段从上一段结束的次日开始，逐段剔除预定到期日越界的事件。每组按事件行等权合并三折；频率、动量、价格基线均保留，滚动组另对每条基线单独拟合同样的滚动截距。

| 组 | 特征 | 校准 | 行数 | Brier | Log loss | 支持档覆盖率 | 开发期状态 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| A | V2 | 固定 | 2,550 | 0.1057 | 0.3487 | 73.9% | APPROVED |
| B | V2 | 固定 + 滚动 | 2,550 | 0.1076 | 0.3548 | 74.6% | INSUFFICIENT_EVIDENCE |
| C | V3 | 固定 | 2,550 | 0.0917 | 0.3207 | 72.5% | INSUFFICIENT_EVIDENCE |
| D | V3 | 固定 + 滚动 | 2,550 | 0.0950 | 0.3278 | 72.9% | INSUFFICIENT_EVIDENCE |

开发期状态只是折内门槛，不是发布批准；A 在开发期显示 `APPROVED`，但 2026 留出验收仍是校准未通过。

逐折 Brier／log loss：

| 组 | 折 1 | 折 2 | 折 3 |
| --- | --- | --- | --- |
| A | 0.0900 / 0.3311 | 0.0656 / 0.2170 | 0.1838 / 0.5626 |
| B | 0.0841 / 0.3115 | 0.0681 / 0.2261 | 0.1947 / 0.5968 |
| C | 0.0822 / 0.3017 | 0.0593 / 0.1874 | 0.1510 / 0.5385 |
| D | 0.0772 / 0.2836 | 0.0616 / 0.1957 | 0.1660 / 0.5758 |

可结算覆盖率按折为 54.3%、52.6%、45.2%；支持档覆盖率、分档预测与发生率、三条基线的配对 95% 区间和滚动基线对照均在[对照报告](../../reports/index-target-accuracy/comparison/report.md)与 `metrics.json` 中。

### 选型与波浪结构增量

按计划，B/C/D 中两项合并损失均不高于 A 且至少一项严格改善者入选；C、D 满足，按 log loss 选 C。C 相对 A 的 Brier 改善为 0.0140、log loss 改善为 0.0280。

但相对含目标几何的价格基线：

| 组 | Brier 改善（基线 − 模型） | 95% 区间 | Log loss 改善 | 95% 区间 | 结论 |
| --- | ---: | --- | ---: | --- | --- |
| A | +0.0102 | [+0.0022, +0.0186] | +0.0315 | [+0.0143, +0.0463] | 有增量 |
| B | +0.0083 | [−0.0034, +0.0202] | +0.0254 | [−0.0020, +0.0506] | 未建立 |
| C | −0.0056 | [−0.0086, −0.0026] | −0.0189 | [−0.0324, −0.0057] | 负增益 |
| D | −0.0088 | [−0.0136, −0.0042] | −0.0260 | [−0.0417, −0.0099] | 负增益 |

因此 C 虽然按 A 参照入选，但 V3 的波浪特征没有超过价格基线；目标几何是已观察到的有效增量，波浪结构的独立增量未建立。按计划选 C 只表示它满足相对 A 的入选规则，不等于 C 优于价格基线；若进入下一轮，应以“含目标几何的价格基线”作为必须超过的对照，而不是只看 A。

## 候选与前向状态

`reports/index-target-accuracy/comparison/frozen-candidate.json` 保存入选组 C 的特征版本、列顺序、基线列、滚动参数、三折参数、日期网格、快照与源码指纹、开发期指标和选型理由。候选 ID 为 `047b092ed7f260a7533dc0d7d04b242d9f84b7494b364f8ed332069d0a2e3267`。

本轮没有冻结后独立到期的新增样本，因此：

- 状态：`RESEARCH_COMPLETE_AWAITING_HOLDOUT`
- 前向：`AWAITING_NEW_INDEPENDENT_HOLDOUT`
- `frozen_at`：`null`，`final_parameters`：`null`
- 未注册模型；日用摘要继续显示 `forecast_status=UNCALIBRATED`

新的独立留出至少需要 50 条、10 个日期、5 个标的；每个拟发布档至少 50 条、10 个日期、2 个占用时间块，校准差 95% 区间在 ±0.10 内，整体支持档覆盖率至少 50%。计划量仍按约 12–16 个时间块估算。这些沿用原协议，未因本轮实验放宽。

## 验证

从仓库根目录执行：

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
.venv/bin/python scripts/compare_index_target.py \
  --v2-evaluation reports/cn-probability-release-2026/evaluation \
  --v3-evaluation reports/index-target-accuracy/v3-evaluation \
  --output-dir reports/index-target-accuracy/comparison
.venv/bin/python scripts/audit_index_target.py \
  --evaluation reports/cn-probability-release-2026/evaluation \
  --output-dir reports/index-target-accuracy/audit-v2
```

结果：

- `298 passed`；Ruff 检查通过，97 个文件格式通过；`pip check` 无损坏依赖。
- 离线个股 `000333.SZ` 与指数 `000016.SH` 的 `brief --offline --json` 均退出 0、状态 `OK`，摘要显示 `forecast_status=UNCALIBRATED`。
- 对照与审计命令退出 0；文件哈希在各自 `manifest.json`。
- 对照重跑到临时目录后，`metrics.json`、`selection.json`、`frozen-candidate.json` 与主目录逐字节一致，候选 ID 同为 `047b092ed7f260a7533dc0d7d04b242d9f84b7494b364f8ed332069d0a2e3267`。

机器可读证据与哈希见[验收证据](2026-11-19-cn-index-target-accuracy-evidence.json)。本轮未提交 Git，保留工作区修改等待验收。
