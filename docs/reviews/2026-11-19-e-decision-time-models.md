# E 决策时模型可用性与回顾性重算记录

日期 2026-11-19。范围：模型注册表批准/撤回时间线、历史评估只加载决策时已批准模型、新模型重算旧数据的回顾性标记。A–D、F、G 见其他验收记录。

源码指纹 `5a8a4b2aa1bb78ab0f569bb7b6ef764a9bfb50e0f3e3177c437f0589a6908b46`；完整测试 245 项通过。

## 注册表时间线

`ModelRegistry` 的 `history` 保存不可变事件：

```text
{model_id, path, action: APPROVED | WITHDRAWN, at}
```

- `register(model_path, approved_at=...)` 追加 `APPROVED`，并更新 `current/previous`。
- `withdraw(model_id, withdrawn_at=...)` 追加 `WITHDRAWN`，当前模型回退到 `previous`。
- `timeline(scope)` 返回按时间排序的完整时间线。
- `active_at(at, scope, ...)` 返回 `at` 时已批准、未撤回且兼容的最新模型；包含 `model`、`model_id`、`approved_at`、`path`。
- `load_at(at, ...)` 只返回模型本体。
- 旧版注册表没有 `history` 时，从 `current/previous/withdrawn` 种子化近似时间线；新注册写入 schema `1.1`。

## 评估行为

`scripts/evaluate_briefs.py` 新增：

```bash
# 决策时模型：每个决策日加载当时已批准的模型
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol protocol.json --output-dir reports/eval \
  --model-registry reports/models

# 强制模型重算旧数据：标记为回顾性研究
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol protocol.json --output-dir reports/eval-retro \
  --model reports/models/model.json \
  --model-approved-at 2025-01-01T00:00:00+00:00
```

每个 `decisions.jsonl` 行新增：

- `model_id`
- `model_approved_at`
- `retrospective`
- `forecasts`：该决策时模型给出的事件状态/概率（若有）

`summary.json` 和 `manifest.json` 新增 `model_usage`：

```text
study_mode                 DECISION_TIME / RETROSPECTIVE / NONE
decisions_with_model
decisions_without_model
retrospective_decisions
model_ids
```

规则：

- `--model-registry`：只在决策时已批准、未撤回且引擎/配置/周期/品种/feed/复权兼容的模型上推理；决策早于首个批准时间时 `model_id=null`。
- `--model`：强制用于所有决策；`--model-approved-at` 之前的决策标记 `retrospective=true`。
- `--model` 且无批准时间：全部标记 `retrospective=true`，`study_mode=RETROSPECTIVE`。
- 强制重算不改变原始 `decisions.jsonl`；新输出单独目录，作为回顾性研究。

## 烟测

测试模型 `faceecc11471...` 批准时间 `2024-12-27T21:00:00+00:00`，31 个日线决策：

```text
决策时模型：
  study_mode            DECISION_TIME
  decisions_with_model  16
  decisions_without    15
  retrospective         0
  before approval       model_id=null

强制模型 + 批准时间：
  study_mode            RETROSPECTIVE
  decisions_with_model  31
  retrospective         15
  after approval        retrospective=false
```

## 验证

```text
245 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
```

新增测试覆盖：

- 注册表批准/撤回时间线
- `load_at` 在批准前、批准后、撤回后返回正确模型
- `active_at` 返回批准时间
- 评估在决策时只加载当时已批准模型
- 决策早于批准时间时无模型
- 强制模型重算标记回顾性
- 强制模型无批准时间时全部回顾性
- `study_mode` / `retrospective_decisions` 汇总

## 边界

- 决策时可用性依赖注册表时间线；旧注册表种子化的撤回时间只能是近似值。
- 强制模型未提供 `--model-approved-at` 时不能证明批准时间，因此全部按回顾性处理。
- 本次只验证模型可用性和标记，不重新训练或重新校准模型。
- 原始报告和已发出的概率不被重算覆盖；回顾性结果写入独立目录。
- 所有改动未提交，等待验收。
