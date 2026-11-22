# 短期预测增强 PE-1 至 PE-3 实施记录

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)和[预测能力增强路线图](../ROADMAP.md#预测能力增强路线图)。本轮先交付推演、发行契约和事件核销核心；PE-4 至 PE-6 的独立留出、概率批准和全流程复验仍待完成。

## PE-1 浪阶段、信号与目标

- 新增 `project_with_gates(...)`，保留原 `project(...)` 三元组签名。
- 条件角色为 `ADVANCE`、`CURRENT_EVIDENCE`、`INVALIDATION`、`STAGE_EXIT`；推动浪 2/4、锯齿 B、WXY X 的推进条件归 `ADVANCE`，平台 B/C、锯齿 C、WXY Y 归当前证据。
- 新增当前浪标签、当前腿方向和推进方向；完成结构不发行当前浪。
- `DOUBLE_THREE` 过滤双锯齿专属回撤、破 W 和目标，避免把双重三浪套进双锯齿口径。
- `AnalysisContext` 保存按候选 ID 索引的 `Projection`，供摘要和回放共用。

## PE-2 发行数据与终端

- 新增 `schemas/outlook.py`：`BriefOutlook`、`OutlookPeriod`、`OutlookCase`、`OutlookCondition`、`OutlookEvent`，内层版本 `1.0`。
- `Brief.outlook` 为可空字段，旧档案不序列化该字段。
- 日线选择发行后的 5 个交易会话，周线选择发行后开始的 2 个完整交易周；日历不足时返回 `UNAVAILABLE` 和明确原因。
- 每张卡按主备顺序生成 `PRIMARY`、`ALTERNATE_1`、`ALTERNATE_2`；事件区分 `DEFINED` 和 `UNDEFINED`，原因包括 `NO_ADVANCE_GATE`、`NO_STOP`、`NO_VALID_TARGET`。
- JSON 为事实源；终端、Markdown 和 HTML 增加短期观察、当前腿、条件、目标和事件状态。

## PE-3 事件注册、结算与回放

- 新增 `EventSpec` 和 `EVENT_SPECS`，登记 `ADVANCE_FIRST_5D_V1`、`ADVANCE_FIRST_2W_V1`、`OUTLOOK_TARGET_FIRST_5D_V1`、`OUTLOOK_TARGET_FIRST_2W_V1`、`CLOSE_BAND_2W_V1`，并保留既有事件。
- 新增 `evaluate_outlook_event(spec, case, window)`：
  - 推进事件检查 `ADVANCE` 与硬失效/阶段退出。
  - 目标事件支持 `ACTIVE` 和 `AFTER_ADVANCE`；等待推进时目标不计入，推进后下一期开始观察。
  - 同根推进/目标/停止按 `AMBIGUOUS_SAME_BAR`；缺数据、右删失和到期分别记录。
- `evaluate_briefs.py` 的决策行记录 `outlook`，评估网格为每张已发行卡生成新事件行；`ADVANCE_FIRST_*` 和 `OUTLOOK_TARGET_FIRST_*` 已接入 `summary.json` 的事件统计。
- 快速协议验证：`ADVANCE_FIRST_5D_V1` 在完成结构上为 `UNDEFINED_EVENT`，`OUTLOOK_TARGET_FIRST_5D_V1` 可在观察窗内得到 `TARGET_FIRST`。
- 正式入口 `elliottwaver brief 000016.SH --offline --snapshot abfd7531... --config config.advanced.example.yaml --json` 返回 `outlook.status=AVAILABLE`、5 个日线观察期、主备 case 和事件状态。

## 验证

```text
459 passed
ruff check src tests scripts           PASS
ruff format --check src tests scripts  PASS
pip check                              No broken requirements found
```

新增测试：

- `tests/test_outlook.py`：条件角色、完成结构、双重三浪过滤、5 会话观察窗、无卡片降级。
- `tests/test_outlook_events.py`：推进先到、同根歧义、先失效、推进后目标、立即目标、到期和未定义事件。

## 仍未完成

1. PE-4：历史实验协议、四组同样本对照、特征版本 4、`freeze`/`score` 子命令。
2. PE-5：新事件模型范围、批准门槛、`forecasts_for_brief` 的新事件概率展示。
3. PE-6：P-01 至 P-08 全量回归、线上 `settle` 核销、旧档案和 HTML/终端一致性复验。
4. 默认模式仍为 `classic_v4`；新推演随 `brief` 输出，不改变默认数浪。
