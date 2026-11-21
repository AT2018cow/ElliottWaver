# 短期预测增强独立验收修复复验

对应[独立验收](2026-11-21-prediction-outlook-independent-review.md)的 PO-01 至 PO-09。本轮只修复验收指出的工程缺陷；历史评分仍按回顾性研究留存，没有新增 `APPROVED` 概率。

## 修复内容

| 编号 | 修复 | 复验入口 |
| --- | --- | --- |
| PO-01 | 双锯齿 X 浪的 50% 回撤只作为当前浪证据；只有 `Y_DEVELOPING` 且越过 W 终点才产生 `ADVANCE`。X 浪未完成时目标为 `AFTER_ADVANCE` 且事件未定义。 | `tests/test_prediction_review_followup.py::test_wxy_x_retracement_has_no_advance_or_target_gate`、`::test_wxy_x_retracement_down_mirror_has_no_advance` |
| PO-02 | 推演层按亚型、阶段、推进门槛和硬边界过滤目标；平台 B 越界、未完成三角形不再发布通用突破或目标；浪 3 增加严格阶段退出；搜索受限、停止已触发时不发行。旧 `project()` 结果不变。 | `::test_flat_b_beyond_origin_suppresses_c_confirmation_and_target`、`::test_unfinished_triangle_has_stage_exit_and_no_target`、`::test_wave3_has_a_strict_stage_exit_below_wave2_end`、`::test_target_filter_rejects_equal_stop_and_target_before_advance_gate`、`::test_build_outlook_refuses_search_limited_result` |
| PO-03 | 发行按发行时刻过滤完整会话；周中发行从下一个完整周开始，延迟周线不把已开始的周计为首期。`BriefOutlook` 记录 `issued_at`、价格坐标和冻结交易期。回放与 `settle` 读取同一 `outlook.periods`；无 `outlook` 或推演不可用的旧报告保持原事件清单。 | `::test_weekly_outlook_starts_after_the_issue_time`、`::test_delayed_weekly_data_does_not_count_an_already_started_week`、`::test_daily_outlook_skips_a_session_already_open_at_issue`、`::test_settlement_and_replay_share_the_frozen_periods` |
| PO-04 | 训练和评分按事件、候选位置、市场、资产、feed、复权、周期和冻结标的筛选；多队列必须显式指定 `--outlook-slot`、`--market`、`--asset-type`；同一标的/决策/位置重复行报错。 | `::test_train_requires_and_filters_the_outlook_slot`、`::test_score_filters_rows_to_the_frozen_symbol_and_slot` |
| PO-05 | `freeze` 记录实际冻结时刻、开发末次标签可用时刻和开发窗口末端，并拒绝倒填冻结日期。`score` 要求 `HISTORICAL_HOLDOUT` 或 `SEALED_FORWARD_HOLDOUT`，拒绝开发产物重用、训练/校准重叠和早于冻结的决策；未拟合事件保持 `INSUFFICIENT_EVIDENCE` 而不崩溃。历史评分输出 `study_mode=RETROSPECTIVE`、`operational_allowed=false`；新版批准模型缺少冻结候选或留出来源时注册被拒。 | `::test_freeze_rejects_a_backdated_frozen_at`、`::test_score_rejects_a_reused_development_artifact`、`::test_score_keeps_an_untrained_frozen_event_insufficient`、`::test_registry_rejects_new_outlook_approval_without_holdout_provenance` |
| PO-06 | 新事件把 `STAGE_EXIT_FIRST` 和 `AMBIGUOUS_SAME_BAR` 映射为 `NO`；缺行情和未到期仍排除；旧事件标签不变。同根同时触发硬失效和阶段退出时归 `INVALIDATED_FIRST`，两条原因写入 `triggers` 和核销 `evidence`。 | `::test_new_outlook_labels_map_stage_exit_and_ambiguity_to_no`、`::test_same_bar_double_stop_is_hard_invalidation_with_both_reasons` |
| PO-07 | 新增 `ModelRegistry.active_models_at`；`forecasts_for_brief` 按事件和候选位置选择模型，旧模型可与新事件模型并存。`build_brief` 先完成换窗比较再预测，线上和回放传入相同的 `outlook`、日周关系和 `window_state`。 | `::test_brief_uses_models_for_two_events_and_two_slots`、`::test_brief_keeps_an_old_model_alongside_new_outlook_models`、`::test_build_brief_resolves_window_state_before_forecasting`、`::test_replay_event_features_read_the_frozen_window_state` |
| PO-08 | 终端、Markdown 和 HTML 的条件阈值按事件比较精度显示，避免 `22.266` 被显示成 `22.27`；主选/备选、当前方向、目标模式和重新数浪条件改用中文说明。用户手册新增短期观察阅读顺序。 | `::test_console_condition_precision_matches_executable_boundary`、`::test_console_outlook_explains_slot_and_target_mode` |
| PO-09 | 旧分析继续使用原 `project()` 条件、目标和路径；新语义和过滤只进入 `project_with_gates` 和 `outlook` 契约，`result_digest` 不受影响。 | `::test_project_keeps_legacy_analysis_conditions_while_outlook_filters` |

## 独立探针

- 倒填冻结日期：`freeze --frozen-at 2023-12-31...` 对 2026-03-31 结束的开发产物返回 `Frozen time precedes the development window's last available label`。
- 开发目录重用：把开发评估目录传给 `score --holdout`，即使把协议标成 `HISTORICAL_HOLDOUT`，仍因同一 `decisions.jsonl`/`events.parquet` 摘要被拒绝。
- CN 目标评分作用域：旧候选在 CN 留出上只保留 `CN/EQUITY/sina/basis_date_qfq/PRIMARY` 行和 2 个冻结个股，不再混入指数或 synthetic feed。
- 周中发行：AAPL 周线在 2026-11-18 22:00 UTC 发行时，首个完整周从 2026-11-23 开始，2 周收盘为 2026-12-04。
- 日线延迟发行：2026-11-18 18:00 UTC 发行时跳过当天 14:30 UTC 已开盘的会话，首期从 2026-11-19 开始。

## 留出复算

使用当前工作树从冻结快照重新生成 CN/US 日线合并 holdout 和周线 holdout，并重新评分六份冻结候选。候选参数仍来自修复前的开发训练，因此只作回顾性研究。

| 事件 | 市场 | 新行数 | Brier | 频率改善 | 动量改善 | 价格改善 | 状态 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `OUTLOOK_TARGET_FIRST_5D_V1` | CN | 46 | 0.1727 | +0.0703 | +0.1313 | −0.0194 | `INSUFFICIENT_EVIDENCE` |
| `OUTLOOK_TARGET_FIRST_5D_V1` | US | 24 | 0.0828 | +0.1898 | +0.1759 | +0.0092 | `INSUFFICIENT_EVIDENCE` |
| `ADVANCE_FIRST_5D_V1` | CN | 5 | 0.2089 | −0.2060 | −0.2059 | −0.2071 | `INSUFFICIENT_EVIDENCE` |
| `ADVANCE_FIRST_5D_V1` | US | 5 | 0.1989 | +0.2132 | +0.1091 | +0.0278 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | CN | 62 | 0.7310 | −0.1398 | −0.1699 | −0.1510 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | US | 57 | 0.6094 | −0.1902 | −0.2024 | −0.1853 | `INSUFFICIENT_EVIDENCE` |

CN 目标从 214 行降到 46 行，US 目标从 88 行降到 24 行，说明指数和 synthetic feed 已被排除。新事件标签把阶段退出和同根歧义归入 `NO`，因此 US 推进从 3 行增加到 5 行。六项仍全部为 `INSUFFICIENT_EVIDENCE`，没有注册操作性模型。

用当前工作树重新生成开发评估并重新冻结后，CN 目标训练/校准/测试为 72/65/46，Brier 0.1792，价格基线改善 −0.0543；CN 推进为 1/10/2，无法拟合；CN 2 周收盘为 4/33/102，无法拟合。US 2 周收盘为 88/95/57，Brier 0.5373，三条基线改善均为负；US 日线目标为 69/51/24，Brier 0.1311，价格基线改善 +0.0067；US 日线推进为 19/6/7，Brier 0.3241，三条基线改善均为负。六项仍为 `INSUFFICIENT_EVIDENCE`。旧候选的高训练行数来自跨 feed、跨指数和备选位置混样。

## 历史结果处置

旧的六份历史评分是在本轮作用域、观察窗和标签修复前生成的。它们只保留为回顾性研究，不作为批准证据；`score` 新输出会标记 `study_mode=RETROSPECTIVE` 和 `operational_allowed=false`。本轮没有把任何历史候选注册为操作性模型，PE-5 仍为 `INSUFFICIENT_EVIDENCE`。未来前向队列仍为可选。

## 验证

```text
python -m pytest tests -q              512 passed
ruff check src tests scripts           PASS
ruff format --check src tests scripts  PASS
pip check                              No broken requirements found
```

机器可读证据见 [P-01–P-08 与 PO-01–PO-09 矩阵](2026-11-21-prediction-outlook-p01-p08-evidence.json)。
