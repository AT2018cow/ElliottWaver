# 中国市场日线事件概率发布验证

本轮使用 `67f0ca6` 的引擎、特征与发布门槛，沿用每周最后一个交易日的固定决策网格。评估四个事件：5 日与 20 日收盘区间、20 日确认先达、20 日目标先达。期限按实际交易会话计算。个股和指数分别评估，新浪日线的原始数据与复权口径各自保留。

| 用途 | 决策期 |
| --- | --- |
| 训练 | 2023-01-01 至 2024-12-31 |
| 温度校准 | 2025-01-01 至 2025-12-31 |
| 一次性留出 | 2026-01-01 至 2026-11-16 |

结果观察截至 2026-11-16。到期日跨过分段末尾的事件由训练器剔除。2026 年四标的试跑使用过的 600519.SH、000001.SZ、000001.SH、000300.SH 本轮排除。预定的 30 只沪深个股和 7 个指数均取得可校验的新浪快照，实际行情覆盖 2022-07-01 至 2026-11-16。名单、快照 ID、各时间段和发布门槛在回放前固定。[冻结协议](cn-probability-release-2026.json)

本轮用的是冻结的历史行情。个股为手工固定名单，覆盖沪深主板、创业板和科创板，偏向常见、成交活跃的证券，存在幸存者和选样偏差。结论适用于此次证券范围及按周选取的日线决策时点。2026 年资料中个别标的曾用于波浪规则回归，E 模型本轮校验时禁止使用留出事件调整参数。

发布条件沿用既定协议。模型的 Brier 和 log loss 都要优于频率、动量和价格组三条基线，配对块重抽样的 95% 改善区间下界须大于零。可靠性档继续使用原边界、样本数、日期数、两块占用和 ±0.10 误差要求。按各证券类别及事件分别记录通过状态。

## 复现

先确认协议引用的快照仍在 `.cache/eliotbot`。协议的文件 SHA-256 为 `89381795c908e7e599450aac8356486332efbc658e75ec4d20e67f5dbd785cf0`。

```bash
PROTOCOL=docs/protocols/cn-probability-release-2026.json
RUN=reports/cn-probability-release-2026
.venv/bin/python scripts/evaluate_briefs.py --protocol "$PROTOCOL" --output-dir "$RUN/evaluation"
.venv/bin/python scripts/train_forecasts.py \
  --protocol "$PROTOCOL" --decisions "$RUN/evaluation" --output-dir "$RUN/models"
.venv/bin/eliot support-matrix --model-summary "$RUN/models/summary.json"
```

本机运行按单标的分批并行。每批只改协议中的 `samples`，使用 `merge_evaluations.py` 按原完整协议合并，保留生成各批结果的源码指纹。见[使用手册的分批评估示例](../USER_GUIDE.md#48-离线评估与概率研究)。
