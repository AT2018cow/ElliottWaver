# 报告目录说明

日常看盘从 `reports/<标的>/index.html` 进入。要复核一次验证、实验或研究，先按下表找到对应的验收记录，再到本机目录找原始产物。本文档随源码维护，生成的报告、行情快照和缓存不纳入 Git。新克隆通常只有这份说明，看不到表中的本地存档。

## 日常报告

| 路径（相对于 `reports/`） | 用途 |
| --- | --- |
| `<标的>/index.html` | 固定入口，汇总该标的的日线、盘中和详细分析。终端也会给出这个路径。 |
| `<标的>/archive/brief/` | 每次简报的原始归档。`brief.json` 用于历史查阅、核销和研究，实时发行另有 `issuance.json`。 |
| `<标的>/archive/analysis/` | 单周期详细分析归档，例如 `analyze` 输出的图表、规则依据和 JSON。 |
| `watchlist/`、`demo/` | 自选批量入口和演示输出，不是研究存档。 |

查看摘要历史、指定输出根目录或拿单份报告做结算，见[用户手册的报告章节](../docs/USER_GUIDE.md#5-找到并查看报告)。历史验收记录仍可能引用已经清理的旧日常路径，例如 `reports/CN/`、`reports/US/`，这些路径不是当前入口。

## 指数与概率研究

| 本地子目录 | 保存的内容 | 结论与复算入口 |
| --- | --- | --- |
| `index-execution-validation/2026-11-25-spx-spy-v1/` | D 批 SPX 指数信号与 SPY 模拟执行。保存 `protocol.json`、`tool.json`、冻结简报、行情缓存，以及 `scored/` 下的发行格和账本。 | [D 批冻结行情复算](../docs/reviews/2026-11-25-single-index-trading-batch-d.md#真实历史冻结入口)。用于验证执行与账本入口，不是实际成交或收益证据。 |
| `intraday-probability-validation/2026-11-25-spx-v5/` | E 批 SPX 15 分钟和 60 分钟目标先达的开发与留出协议、事件表、候选和评分。 | [E 批真实历史复算](../docs/reviews/2026-11-25-single-index-trading-batch-e.md#真实历史复算)。两组样本均为 `INSUFFICIENT_EVIDENCE`，未获批发布概率。 |
| `prediction/outlook/` | 短期推演的中美开发、冻结候选、历史留出、模型评分及各轮复验。`history-*` 与 `review-*` 对应不同实验轮次。 | [开发与候选冻结](../docs/reviews/2026-11-21-prediction-outlook-protocols.md) · [历史留出与复算](../docs/reviews/2026-11-21-prediction-outlook-history-holdout.md) · [独立复验](../docs/reviews/2026-11-21-prediction-outlook-review-followup.md) |
| `index-target-accuracy/` | A 股指数 20 日目标先达。包括点位与失效审计、价格基线比较、历史压力测试及 2026 年留出重算。 | [点位与目标先达审计](../docs/reviews/2026-11-19-cn-index-target-accuracy.md) · [概率留出重算](../docs/reviews/2026-11-20-cn-index-probability-recheck.md) |
| `cn-probability-release-2026/` | 沪深个股和中国指数概率的留出评估、训练产物和盲标试跑。 | [初版留出记录](../docs/reviews/2026-11-19-cn-probability-release.md) · [后续重算](../docs/reviews/2026-11-20-cn-index-probability-recheck.md) |

研究结果按各记录所列的样本、数据源和代码基线解读。冻结行情回放可以复核历史计算，但不能替代前向发行回执、独立留出标签或实盘成交证据。版本化协议与前向验证入口见[评估协议索引](../docs/protocols/README.md)。

## 工程验收与波浪实验

| 本地子目录 | 保存的内容 | 记录入口 |
| --- | --- | --- |
| `validation/`、`validation-ext5/`、`validation-ext5-fixed/`、`validation-20261118-ext5-v3/` | 早期个股、指数及 ETF 的冻结报告和第五浪修改前后对照。 | [首轮扩展验收](../docs/reviews/2026-11-17-deepseek-ext5-acceptance.md) · [修复复验](../docs/reviews/2026-11-17-deepseek-ext5-fixed.md) · [v3 清单](../docs/validation_20261118_ext5_v3.json) |
| `validation-cn-index/`、`validation-20261118-cn-index-v3/`、`validation-long-history/`、`validation-20261118-long-history-v3/` | A 股指数和长历史日周线的冻结验收报告。 | [指数 v3 清单](../docs/validation_20261118_cn_index_v3.json) · [长历史 v3 清单](../docs/validation_20261118_long_history_v3.json) |
| `validation-engine-fix/`、`wave-engine/` | 引擎修复复验和多浪级增强验收，包含亚型样例、候选召回对照及性能基准。 | [引擎修复](../docs/reviews/2026-11-18-engine-fix.md) · [多浪级引擎验收](../docs/reviews/2026-11-20-wave-engine-acceptance.md) |
| `independent-p3-20261118/`、`parent-subwave-fix-20261118/`、`deepseek-review-20261118/` | 父子浪证据、三角形子浪规则及相应冻结复验。 | [父子浪验收](../docs/reviews/2026-11-18-parent-subwave-acceptance.md) · [修复复验](../docs/reviews/2026-11-18-parent-subwave-fix.md) · [三角形子浪验收](../docs/reviews/2026-11-18-triangle-children-acceptance.md) |
| `cn-triangle-experiment/`、`cn-triangle-experiment-allow-gaps/`、`cn-triangle-experiment-followup/` | A 股三角形子浪固定样本试跑、缺口敏感性和后续预检。 | [试跑结果](../docs/reviews/2026-11-18-cn-triangle-experiment-results.md) · [后续对照](../docs/reviews/2026-11-18-cn-triangle-experiment-followup-results.md) |
| `cn-triangle-classic-v6/`、`cn-triangle-classic-v6-reviewed/` | 经典三角形 v6 独立实验及复核。 | [v6 实验记录](../docs/reviews/2026-11-18-cn-triangle-classic-v6.md) |
| `roadmap-repair-2026/`、`us-acceptance/` | 路线图修复的决策和模型校验产物，以及美股行情来源和分析工作流的验收产物。 | [路线图修复](../docs/reviews/2026-11-19-roadmap-repair.md) · [美股验收](../docs/reviews/2026-11-20-us-equity-acceptance.md) |

本机还留有少量辅助结果，查阅时以目录内的协议和清单为准。

- `cn-triangle-experiment-followup-independent/` 和 `cn-triangle-experiment-followup-smoke2/` 保存三角形后续探针及小样本试跑。`cn-triangle-classic-v6-final/` 只保存协议，不能当作已经产出评分。
- `.mvp-live-check.2ngflM/`、`live-smoke/` 和 `nvda-advanced/` 保存本地运行样例，包括历史简报联调、多周期分析和 NVDA 回放。根目录还有个别回归 JSON 和科创 50 示例图。这些可作定位线索，不能单独作为当前版本的验收结论。

## 查阅与保留

1. 想看结论，先开上表的验收记录或[完整验收索引](../docs/reviews/README.md)，核对日期、源码基线、数据源和样本范围。
2. 想核对数字，打开本机实验的 `summary.json`、`manifest.json` 或对应验收记录列出的证据文件。日常网页和研究产物分开保存，不需要从标的页面逐份翻找。
3. 想复算，按记录使用原协议、行情快照和对应代码基线，输出到新的子目录。如果原快照或实际发行回执已经缺失，应记录为无法原样复现。重新下载的历史行情可能有供应商修订，只能作为新一轮输入。

清理磁盘时，日常图表和临时运行输出可与研究证据分开处理。需要继续复核的试验应一并保留协议、输入快照、清单及评分结果，若复验依赖实际发行时间，还应保留原报告和回执。不能假设本地存档可从 Git 恢复。
