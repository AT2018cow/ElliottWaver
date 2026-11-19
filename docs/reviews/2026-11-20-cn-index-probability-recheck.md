# 指数目标先达修复与历史复验

日用个股和指数摘要已通过冻结快照烟测。正式概率仍待验收。本次修复了标签可用时间、V2/V3 输入校验、预测明细的候选 ID、浪 5 失效审计和已结算计数。历史复跑还发现通用训练入口传错了候选特征字段，导致 V3 报缺列，V2 把候选特征默认为零。训练行和特征提取现使用同一字段。

## 原实验复验

- 2023–2025 四组的指标、选型和 C 组研究候选 ID 逐字节一致。重新导出的 10,200 条预测都能按 `candidate_key` 对回原候选。
- 2026 年边界审计的 3,138 条事件中，1,590 条可结算，1,548 条未定义。按候选 ID 重放的 154 条推动浪 5 均未触发硬失效。逐例浪长、上限和复算来源保存在审计 JSON 中。

原始评估的输入快照与分段沿用[开发期记录](2026-11-19-cn-index-target-accuracy.md)。重新生成的对照与审计保存在 `reports/index-target-accuracy/comparison/` 和 `reports/index-target-accuracy/audit-v2/`。

## 2022 年历史压力测试

历史复验沿用原日线快照和每周最后交易日网格。2021 年有 927 条训练事件，2022 年上半年有 694 条校准事件，下半年有 627 条已到期且结果明确的测试事件，覆盖 22 个决策日和 7 个指数。较晚上市的指数在早期分段有更短的历史。每个分段只纳入在段内到期的事件。[七指数历史协议](../protocols/cn-index-target-history-2021-2022.json)

| 固定带截距校准 | 模型 Brier | 模型 Log loss | 价格组 Brier | 价格组 Log loss | 支持档覆盖率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| V2 | 0.1379 | 0.4132 | 0.1564 | 0.4610 | 0% |
| V3 | 0.1266 | 0.3701 | 0.1241 | 0.3709 | 51.5% |

V3 的两项模型损失低于 V2。V3 价格组也加入相同的目标几何特征。V3 相对该价格组的两项改善区间都跨零，未建立波浪特征的额外收益。V2 相对价格组的 Brier 改善区间也跨零。分块检验和完整指标在 `reports/index-target-accuracy/historical-stress/fixed_calibration.json`。

## 2026 年留出重算

用修复后的通用训练入口复算 30 只沪深个股和 7 个指数，8 项日线事件仍为 `INSUFFICIENT_EVIDENCE`。指数目标先达有 1,590 条验证事件。模型 Brier 为 0.1863，同样使用正确特征的价格组为 0.1854，模型未通过价格基线对照，支持档覆盖率仍为 0%。这些重算指标取代初版报告中使用缺失候选特征得到的指标。

同一历史区间用已冻结 V2 候选及其冻结基线作诊断时，模型 Brier 为 0.1741，价格组为 0.1854。两项配对改善区间均跨零，支持档覆盖率为 0%。评分产物标记为 `RETROSPECTIVE_RESEARCH`。正式评分只纳入决策日晚于冻结日且已到期的事件。[前向协议](../protocols/cn-index-target-forward.md)

## 程序验证

305 项测试通过，Ruff、依赖和差异检查通过。使用 `000333.SZ` 和 `000016.SH` 的冻结行情跑 `eliot brief --offline`，均返回 `OK`。默认支持矩阵显示 `UNCALIBRATED`。

## 复现

```bash
HISTORY=docs/protocols/cn-index-target-history-2021-2022.json
ROOT=reports/index-target-accuracy

.venv/bin/python scripts/evaluate_briefs.py --protocol "$HISTORY" \
  --output-dir "$ROOT/historical-stress/evaluation"
.venv/bin/python scripts/train_forecasts.py --protocol "$HISTORY" \
  --decisions "$ROOT/historical-stress/evaluation" --output-dir "$ROOT/historical-stress/scored"
.venv/bin/python scripts/train_forecasts.py --protocol docs/protocols/cn-probability-release-2026.json \
  --decisions reports/cn-probability-release-2026/evaluation --output-dir "$ROOT/2026-training-recheck"
```

通用训练命令用温度校准。表中的固定带截距对照复用 `prepare_index_target._fit` 的 `binary_logit_affine`，按同一历史协议分别取 V2 和 V3 列顺序，模型、动量和价格组共用该校准方式。原实验复跑的对照命令见[开发期记录](2026-11-19-cn-index-target-accuracy.md#验证)。
