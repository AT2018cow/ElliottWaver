# 短期预测增强 PE-4 实施记录

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)的 PE-4。PE-1 至 PE-3 的推演、发行契约和事件核销见[上一轮记录](2026-11-21-prediction-outlook-pe1-pe3.md)。独立验收后的样本作用域、冻结/留出封存和标签修复见[修复复验](2026-11-21-prediction-outlook-review-followup.md)；本文记录的旧评分产物只作回顾性研究。

## 特征版本 4

- `FEATURE_VERSION` 保持旧默认 `2`，`SUPPORTED_FEATURE_VERSIONS` 增加 `4`，旧模型继续按自身顺序读取。
- 新事件 `ADVANCE_FIRST_*`、`OUTLOOK_TARGET_FIRST_*` 在版本 4 下追加：
  - 推进、硬失效、阶段退出边界到发行价的 ATR 距离；
  - 目标区上下界 ATR 距离和 `ACTIVE`/`AFTER_ADVANCE` 模式；
  - 当前腿方向、推进方向、双重三浪、子浪状态/深度、日周关系和换窗状态。
- 候选事件行在 `evaluate_briefs.py` 中写入 `outlook_features`；价格基线只取价格几何，波浪模型取完整特征。

## 四组同样本对照

`train_forecasts.py` 对同一训练/校准/测试样本拟合四组：

| 组 | 内容 |
| --- | --- |
| A 频率 | 训练段类别频率 |
| B 动量 | `log_return_20` |
| C 价格几何 | 价格、ATR、推进/失效/目标距离 |
| D 波浪 | C 加模板、阶段、子浪、日周和换窗证据 |

`forecast_evidence` 对 D 相对 A/B/C 的 Brier、log loss 和配对区间逐项记录；未通过门槛时保留 `INSUFFICIENT_EVIDENCE` 和原因。

## freeze / score

```bash
.venv/bin/python scripts/train_forecasts.py freeze \
  --protocol DEV.json --decisions reports/prediction/dev \
  --output reports/prediction/candidate.json
.venv/bin/python scripts/train_forecasts.py score \
  --candidate reports/prediction/candidate.json \
  --holdout reports/prediction/holdout \
  --output-dir reports/prediction/model
```

- `freeze` 用开发评估训练并写出带 `candidate_id` 的 `FROZEN_CANDIDATE`，保留参数、特征顺序、baselines、协议/引擎/配置摘要和样本清单。
- `score` 校验候选摘要、引擎/配置/特征版本、事件集合、holdout 起始晚于冻结时刻，并只评分 `grid.start..end` 且到期不超过 `outcome_as_of` 的事件。
- 缺少 holdout 行时逐事件写 `INSUFFICIENT_EVIDENCE`，不把开发期指标当作前向结果。

## 验证

```text
464 passed
ruff check / format / pip check: PASS
```

- `tests/test_prediction_freeze.py`：特征版本 4、outlook 特征编码、freeze 候选摘要、score 非法摘要和 holdout 早于冻结的拒绝。
- 新鲜开发评估上运行 `freeze`，生成包含 4 个事件的 `FROZEN_CANDIDATE`；v4 候选的事件特征顺序包含 16 个 `outlook_*` 特征。
- 当前没有冻结时刻之后的成熟 holdout 行情，因此 `score` 的正式前向结果仍为 `INSUFFICIENT_EVIDENCE`；这是预期状态，不是代码失败。

## 仍未完成

1. PE-5：新事件模型作用域、批准门槛接线和 `forecasts_for_brief` 的新事件概率展示。
2. PE-6：P-01 至 P-08 全量回归、线上 `settle` 核销、旧档案与终端/HTML 一致性复验。
3. 独立前向留出需要未来行情快照；当前只能完成开发/校准和接口验收。
