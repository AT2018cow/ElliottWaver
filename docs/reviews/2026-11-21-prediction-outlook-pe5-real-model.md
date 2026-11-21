# PE-5 真实评分模型与 P-01–P-08 证据矩阵

PE-5 采用“正式记录 INSUFFICIENT_EVIDENCE + 用真实评分模型走 brief 概率路径”的方案，不伪造获批模型。

## 真实评分模型

独立验收后重新生成留出并评分，使用新评分模型。旧 `history-score-*` 产物仍保留为修复前研究，不再作为本路径证据。

| 市场 | 模型 | 事件 | 状态 | 回顾性 |
| --- | --- | --- | --- | --- |
| CN | `reports/prediction/outlook/review-score3-cn-target/model.json` | `OUTLOOK_TARGET_FIRST_5D_V1` | `INSUFFICIENT_EVIDENCE` | `RETROSPECTIVE` / 禁止操作性发布 |
| US | `reports/prediction/outlook/review-score3-us-target/model.json` | `OUTLOOK_TARGET_FIRST_5D_V1` | `INSUFFICIENT_EVIDENCE` | `RETROSPECTIVE` / 禁止操作性发布 |

使用与训练一致的配置运行正式入口：

```bash
.venv/bin/eliot brief 600036.SH --offline \
  --snapshot b2243746e0528404e410da64bb9d44fb261761a393853f97aebc240a6eb79e81 \
  --config <matching-cn-config> \
  --model reports/prediction/outlook/review-score3-cn-target/model.json \
  --no-weekly --no-window-check --format json --json
```

CN 结果：

```text
status = OK
outlook.status = AVAILABLE
forecast_status = INSUFFICIENT_EVIDENCE
card forecast = OUTLOOK_TARGET_FIRST_5D_V1 / INSUFFICIENT_EVIDENCE / PRIMARY
reason = test rows 46 < 50, test symbols 2 < 5, frequency/price baseline CI lower bound <= 0, ...
```

US 使用 `AAPL`（`cff553...`，`.cache/us-acceptance`）和对应模型：

```text
status = OK
outlook.status = AVAILABLE
forecast_status = INSUFFICIENT_EVIDENCE
card forecast = OUTLOOK_TARGET_FIRST_5D_V1 / INSUFFICIENT_EVIDENCE / PRIMARY
reason = test rows 24 < 50, test symbols 2 < 5, price baseline CI lower bound <= 0, ...
```

注意：`300750.SZ` 的开发快照 `3cbd49e6...` 行情只到 2026-03-31，发行截止晚于最后行情，新观察窗按设计返回 `CALENDAR_UNAVAILABLE`/`UNAVAILABLE`，不会把已过去的 4–11 月当作未来窗口。这直接验证了 PO-03 的修复。

这说明：

- 真实评分模型能被 `build_brief` 加载并按 `outlook_slot` 找到卡片；
- 未获批时卡片显示状态和原因，不显示概率数字；
- 获批后才显示 `APPROVED` 概率，路径与合成模型测试一致。

## P-01–P-08 证据矩阵

| 用例 | 证据 |
| --- | --- |
| P-01 当前腿/推进方向，涨跌镜像 | `tests/test_outlook.py::test_project_with_gates_assigns_impulse_advance_role`、`test_project_with_gates_down_mirror` |
| P-02 完成结构、双重三浪隔离、阶段退出严格边界、浪 5 上限 | `test_project_with_gates_assigns_completed_stage_exit_only`、`test_project_with_gates_suppresses_double_zigzag_specific_evidence`、`test_stage_exit_is_strict_at_the_boundary`；`tests/test_engine.py` 浪 5 上限 |
| P-03 推进/目标/停止先到与同根歧义 | `tests/test_outlook_events.py` 8 个用例 |
| P-04 5 日/2 周观察窗、当前周跳过、缺交易日、早收/时区、假期、显式截止 | `test_build_outlook_has_five_sessions_and_cases`、`test_build_outlook_weekly_has_two_periods`、`test_weekly_outlook_skips_the_current_week`、`test_daily_outlook_skips_holiday_sessions`、`test_outcome_window_explicit_cutoff_is_right_censored`、`test_link_timeframes_tolerates_a_missing_daily_session` |
| P-05 CN/US 拆股/复权核销 | `tests/test_prediction_pe6.py::test_cn_split_settlement_matches_unsplit`、`tests/test_review_followup.py::test_provider_split_uses_verified_issuance_basis` |
| P-06 发行/回放/核销同事件键、旧报告 | `tests/test_prediction_pe6.py::test_settle_brief_records_outlook_events`、`test_old_report_without_outlook_reads_and_renders`、`test_previous_brief_replay_preserves_outlook` |
| P-07 作用域/批准时间/单事件单槽/按事件查询 | `test_outlook_model_requires_scope_fields`、`test_new_outlook_model_approval_requires_completed_holdout`、`test_validate_model_rejects_multiple_new_outlook_events`、`test_validate_model_requires_symbol_slot_for_close_event`、`test_registry_queries_by_event_and_outlook_slot` |
| P-08 四标的 brief/watchlist/history/settle/support-matrix 与终端/HTML/JSON 一致 | [P-08 全流程](2026-11-21-prediction-outlook-p08.md)、`tests/test_prediction_pe6.py` |

## 结论

- PE-5 的形式验收为 `INSUFFICIENT_EVIDENCE`：三类事件、两个市场的历史 holdout 都未达到发布门槛。
- 真实评分模型的 brief 路径已演示；没有伪造 `APPROVED` 概率。新评分模型带 `study_mode=RETROSPECTIVE` 和 `operational_allowed=false`，只能用于回顾性展示，不能注册为操作性发布。
- P-01–P-08 的证据已按用例列出；剩余是统计改进，不是流程缺失。
- 未来前向队列保留，但不是本轮完成条件。
