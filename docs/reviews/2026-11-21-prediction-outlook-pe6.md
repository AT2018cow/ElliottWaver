# 短期预测增强 PE-6 回归与回放记录

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)的 PE-6。PE-1 至 PE-5 见[推演与事件](2026-11-21-prediction-outlook-pe1-pe3.md)、[冻结/评分](2026-11-21-prediction-outlook-pe4.md)和[概率展示](2026-11-21-prediction-outlook-pe5.md)。

## 已覆盖回归

| 用例 | 证据 |
| --- | --- |
| P-01 推动浪 2/4、3/5 的当前腿和推进方向 | `tests/test_outlook.py` 条件角色、上涨和下跌镜像断言 |
| P-02 完成结构、双重三浪隔离、阶段退出严格边界 | `test_project_with_gates_assigns_completed_stage_exit_only`、`test_project_with_gates_suppresses_double_zigzag_specific_evidence`、`test_stage_exit_is_strict_at_the_boundary` |
| P-03 推进/目标/停止的先到顺序、同根歧义 | `tests/test_outlook_events.py` 8 个用例 |
| P-04 5 个交易日/2 个完整周观察窗、当前周跳过、日历不足 | `tests/test_outlook.py` 5 会话与 2 周观察窗；`test_weekly_outlook_skips_the_current_week`；日历不足返回 `UNAVAILABLE` |
| P-05 拆股/复权核销 | 既有 `tests/test_review_followup.py` 拆股发行基线和结算测试 |
| P-06 发行、回放、核销同一事件键 | `tests/test_prediction_pe6.py` 结算记录包含 `ADVANCE_FIRST_5D_V1` 和 `OUTLOOK_TARGET_FIRST_5D_V1` |
| P-07 模型作用域/批准拒绝/批准时间 | `tests/test_outlook_forecast.py` 缺少 `outlook_version` 时拒绝、holdout 未结束不能批准；`tests/test_forecasts.py` 注册/回退 |
| P-08 终端/HTML/JSON 一致、批准模型卡片概率 | `tests/test_prediction_pe6.py` 三处均包含“短期观察”；`test_build_brief_shows_approved_target_probability_on_card` |

## 本轮修复

- `settle_brief` 对新事件改用 `evaluate_outlook_event`，不再套用旧 `first_touch`；卡片事件从 `Brief.outlook.cases` 取条件、目标和事件状态。
- `event_specs` 加入 `CLOSE_BAND_2W_V1`、`ADVANCE_FIRST_*` 和 `OUTLOOK_TARGET_FIRST_*`，核销行与摘要卡片使用同一 `scenario_id`。
- `support-matrix` 加入新事件和 `outlook_version`/`outlook_slot` 字段。
- `brief` 的终端、Markdown、HTML 和 JSON 都从同一个 `BriefOutlook` 渲染。

## 验证

```text
471 passed
tests/test_prediction_pe6.py: 3 passed
ruff check / format / pip check: PASS
```

## 仍未完成

1. P-08 的真实样本全流程已用四个冻结快照完成，记录见[P-08 全流程](2026-11-21-prediction-outlook-p08.md)。
2. 新事件正式概率批准仍需冻结时刻之后的独立 holdout；当前没有可用未来行情。
3. 旧档案/旧模型的完整迁移矩阵可继续扩充，但现有 `tests/test_forecasts.py`、`tests/test_settlement.py` 和 `tests/test_review_followup.py` 已覆盖兼容读取和回退。
