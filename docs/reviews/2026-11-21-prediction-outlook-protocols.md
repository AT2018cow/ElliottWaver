# 短期预测增强 PE-4/PE-5 实验协议与队列

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)的 PE-4/PE-5。本轮提交开发协议、独立 holdout 队列和冻结候选；正式 holdout 评分等待冻结时刻之后的行情快照。后续历史 holdout 和独立验收修复见[历史 holdout](2026-11-21-prediction-outlook-history-holdout.md)与[修复复验](2026-11-21-prediction-outlook-review-followup.md)；本文的早期 dry-run 命令需补 `--market/--asset-type/--timeframe/--feed/--adjustment`。

## 开发协议

| 协议 | 市场 | 样本 | 缓存 |
| --- | --- | --- | --- |
| `prediction-outlook-dev-cn-v1.json` | CN | `600519.SH`（个股）、`000016.SH`（指数） | `.cache/elliottwaver` |
| `prediction-outlook-dev-us-v1.json` | US | `AAPL`、`SPY` | `.cache/us-acceptance` |

- 事件：`ADVANCE_FIRST_5D_V1`、`OUTLOOK_TARGET_FIRST_5D_V1`、`CONFIRM_FIRST_20D_V1`、`TARGET_FIRST_20D_V1`、`CLOSE_BAND_5D_V1`。
- 特征版本：`4`。
- 引擎：`hierarchical_v1`，`max_subwave_depth=1`，尺度 `1.0/1.4/2.4/3.8`。
- 分段：训练 2023-01-01–2024-06-30；校准 2024-07-01–2025-12-31；测试 2026-04-01–2026-09-30。
- 发布门槛写入协议 `release`，与 `train_forecasts.DEFAULT_RELEASE` 一致。

评估产物：

```text
reports/prediction/outlook/dev-cn/
reports/prediction/outlook/dev-us/
```

## 冻结候选

```bash
.venv/bin/python scripts/train_forecasts.py freeze \
  --protocol docs/protocols/prediction-outlook-dev-cn-v1.json \
  --decisions reports/prediction/outlook/dev-cn \
  --output reports/prediction/outlook/candidate-cn.json \
  --event ADVANCE_FIRST_5D_V1 --outlook-slot PRIMARY
```

| 候选 | feature_version | scope | 样本（train/cal/test） | 状态 |
| --- | --- | --- | --- | --- |
| `candidate-cn.json` | 4 | CN/EQUITY/1d，`PRIMARY`，outlook 1.0 | 48 / 23 / 10 | `INSUFFICIENT_EVIDENCE` |
| `candidate-us.json` | 4 | US/EQUITY/1d，`PRIMARY`，outlook 1.0 | 39 / 57 / 12 | `INSUFFICIENT_EVIDENCE` |

两个候选都成功冻结并带 `candidate_id`；状态不足来自样本量/基线和支持档门槛，不是协议或作用域错误。

## 独立 holdout 队列

| 协议 | 市场 | 状态 |
| --- | --- | --- |
| `prediction-outlook-holdout-cn-v1.json` | CN | `AWAITING_FORWARD_SNAPSHOT` |
| `prediction-outlook-holdout-us-v1.json` | US | `AWAITING_FORWARD_SNAPSHOT` |

- 队列使用占位快照 ID（64 个 0），取得冻结时刻之后的快照后替换。
- 目标窗口 2026-12-01–2026-12-31；CN 的 XSHG 日历当前只覆盖到 2026 年，若窗口超出需先更新日历覆盖。
- 评分入口：

```bash
.venv/bin/python scripts/evaluate_briefs.py --protocol <holdout> --output-dir <holdout-eval>
.venv/bin/python scripts/train_forecasts.py score \
  --candidate reports/prediction/outlook/candidate-cn.json \
  --holdout <holdout-eval> --output-dir reports/prediction/outlook/model-cn
```

- 用现有快照做过一次接线 dry-run：`score` 可运行，结果仍为 `INSUFFICIENT_EVIDENCE`；该 dry-run 不构成独立留出证据。

## 验证

```text
512 passed
ruff check / format / pip check: PASS
```

## 历史 holdout

按用户要求改用历史数据，开发段截止 2026-03-31、holdout 为 2026-04-01–2026-11-18，已完成评分。结果见[历史 holdout](2026-11-21-prediction-outlook-history-holdout.md)。目标先达模型未达发布门槛，当前没有获批模型。未来前向队列仍保留，但不是本轮完成条件。

独立验收后新增合并协议 `prediction-outlook-history-review-{dev,holdout}-{cn,us}-daily-v1.json`，一次评估同时生成推进、目标、确认、20 日目标和 5 日收盘事件；`train`/`freeze` 接受评估 manifest 的事件集合为训练协议的父集。周线继续使用 `*-weekly-v1.json`。
