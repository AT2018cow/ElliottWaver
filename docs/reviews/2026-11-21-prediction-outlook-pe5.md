# 短期预测增强 PE-5 实施记录

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)的 PE-5。PE-1 至 PE-4 见[推演与事件记录](2026-11-21-prediction-outlook-pe1-pe3.md)和[冻结/评分记录](2026-11-21-prediction-outlook-pe4.md)。独立验收后的批量模型查询、单事件/单槽位、冻结来源和回顾性发布限制见[修复复验](2026-11-21-prediction-outlook-review-followup.md)。

## 模型作用域

- 新事件模型在 `scope` 中要求：
  - `outlook_version: "1.0"`；
  - `outlook_slot` 为 `PRIMARY`、`ALTERNATE_1` 或 `ALTERNATE_2`。
- `validate_model(..., operational=True)` 对 `ADVANCE_FIRST_*` 和 `OUTLOOK_TARGET_FIRST_*` 检查上述字段；缺少时拒绝注册。
- `ModelRegistry._compatible` 支持按 `outlook_version` 和 `outlook_slot` 过滤。
- 旧事件模型不需要新字段，继续按原作用域读取。

## 概率展示

`forecasts_for_brief` 新增 outlook 分支：

1. 按模型 `outlook_slot` 选择 `BriefOutlook.cases` 中的对应卡片。
2. 校验该卡片的 `OutlookEvent` 为 `DEFINED`；未定义时输出 `UNDEFINED_EVENT` 和原因。
3. 用与训练相同的 `candidate_features + outlook_features` 构造特征向量。
4. 预测、支持档检查和 `BriefForecast` 输出；附带 `outlook_version`、`outlook_slot` 和 `scenario_id`。
5. `build_brief` 把预测按 `scenario_id` 附加到对应 `BriefCard.forecasts`，终端/JSON/Markdown/HTML 可读。

## 支持矩阵

`support-matrix` 新增：

- `CLOSE_BAND_2W_V1`；
- `ADVANCE_FIRST_5D_V1` / `ADVANCE_FIRST_2W_V1`；
- `OUTLOOK_TARGET_FIRST_5D_V1` / `OUTLOOK_TARGET_FIRST_2W_V1`。

每行输出模型作用域中的 `outlook_version` 和 `outlook_slot`，便于区分主选/备选模型。

## 验证

```text
468 passed
tests/test_outlook_forecast.py: 4 passed
ruff check / format / pip check: PASS
```

新增测试：

- 新事件模型缺少 outlook scope 时 `validate_model` 拒绝，完整 scope 通过。
- `forecasts_for_brief` 按 `PRIMARY` 找到卡片，事件 DEFINED 时返回 `APPROVED` 概率和 `outlook_slot/outlook_version`。
- `build_brief` 将预测附加到对应卡片。
- `support_matrix` 列出新事件及 outlook 作用域。

## 仍未完成

1. PE-6：P-01 至 P-08 全量回归、线上 `settle` 回放、旧档案/终端/HTML 一致性复验。
2. 正式前向 holdout 仍需要冻结时刻之后的行情快照；当前只有接口和开发/校准证据。
3. 新事件模型的实际批准仍需独立留出满足发布门槛。
