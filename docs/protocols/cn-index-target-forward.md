# 指数 20 日目标先达的候选与前向验收

本轮只推进 `TARGET_FIRST_20D_V1`。适用范围为冻结名单中的 7 个中国市场指数、日线、`sina-index` 无复权行情。按每周最后一个交易日取决策，事件以候选首次达到目标区或先失效的次序为准。校准使用已到期且结果明确的候选事件。[前序验收](../reviews/2026-11-19-cn-probability-release.md)

## 已冻结的参数

在 2023–2025 开发期做三个滚动折。各折依次拟合基础模型、校准、检验。去除到期日越过段尾的行。候选温度法和带截距的二元对数几率校准按折内样本数加权的 log loss 选优。频率、动量、价格组三个对照也冻结。

| 方法 | 开发验证行 | log loss | Brier |
| --- | ---: | ---: | ---: |
| 温度 | 2,550 | 0.35563 | 0.10556 |
| 带截距的校准 | 2,550 | 0.34875 | 0.10572 |

按预定 log loss 选中带截距的校准。最终参数用 2023–2024 的 3,477 个训练事件和 2025 年的 1,838 个校准事件重新拟合。候选 ID 为 `e3e7202dbc3665cfdba5fdcb537ad1d01bca04cf49b11963be4432328e9c77a3`。上海时间 2026-11-19 冻结。[候选、参数与各折证据](cn-index-target-frozen-2026.json)

V3 目标几何与滚动校准是独立的开发期研究，见[开发期验收](../reviews/2026-11-19-cn-index-target-accuracy.md)。该轮没有新增独立留出，状态为研究完成、待新留出，不替换本节的 V2 冻结候选。

## 新留出

首次评分前固定网格起止日、快照、结果观察日和发布门槛。时间范围从候选冻结日后开始，沿用同一指数名单和原门槛。计划样本量约 12–16 个时间块，按前一次的事件密度约 2,900–3,800 条已到期事件。优先覆盖不同市场环境。块长依实际期限重叠计算，最终审批按三项基线改善、可靠性和支持档覆盖率判定。

用 `data fetch` 保存新日线，使用[现有冻结协议](cn-probability-release-2026.json)的配置创建前向协议。仅保留候选 JSON 的七个指数，填入新快照 ID，将事件列表设为 `["TARGET_FIRST_20D_V1"]`，并将 `grid` 的 `start`、`end`、`outcome_as_of` 设置为锁定的范围。最终测试行的预定到期日须落在观察日以内。评分时使用冻结的参数和三条对照。

```bash
CANDIDATE=docs/protocols/cn-index-target-frozen-2026.json
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol reports/index-target-forward/protocol.json \
  --output-dir reports/index-target-forward/evaluation
.venv/bin/python scripts/prepare_index_target.py score \
  --candidate "$CANDIDATE" \
  --holdout reports/index-target-forward/evaluation \
  --output-dir reports/index-target-forward/scored
.venv/bin/eliot support-matrix \
  --model-summary reports/index-target-forward/scored/model.json
```

`score` 验证候选和评估清单，拒绝冻结日前的决策范围，按同一指数、数据口径、特征版本和配置评分。新留出通过实际门槛后，产出的标准模型可注册给日常摘要，并只对经验证的指数名单加载。开发验证结果和已使用的 2026 年留出保持存档。
