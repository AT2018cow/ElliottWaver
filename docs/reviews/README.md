# 代码验收记录

验收记录只描述所列基线和源码指纹对应的改动。测试通过、问题记录、修复完成和统计有效性分别记录，不能相互替代。

A 股三角形子浪对照已完成固定样本试跑。后续实施与复验见[试跑复核与下一步](2026-11-18-cn-triangle-experiment-followup.md)。

| 日期 | 范围 | 结论 | 证据 |
| --- | --- | --- | --- |
| 2026-11-18 | 三角形 `subwave-v6` 修复与默认组 | 正式认证采用 A 组，修复子浪备选搜索、回放复权坐标和产物字段。 | [修复与决策](2026-11-18-cn-triangle-classic-v6.md) |
| 2026-11-18 | A 股三角形后续实验独立验收 | 部分通过。价格无分歧，复权坐标与整组候选选择待修复。 | [独立验收](2026-11-18-cn-triangle-followup-acceptance.md) |
| 2026-11-18 | A 股三角形规则复核与正式认证实现记录 | 自验 176 项测试。小回放无价格分歧。价格标签和结构认证的独立复核见上行。 | [复核结果](2026-11-18-cn-triangle-experiment-followup-results.md) |
| 2026-11-18 | A 股三角形子浪规则对照实验 | 固定样本试跑零分歧。E 腿父级归因和缺口口径待修正。 | [结果](2026-11-18-cn-triangle-experiment-results.md)、[协议](2026-11-18-cn-triangle-experiment-protocol.md)、[独立复核](2026-11-18-cn-triangle-experiment-followup.md) |
| 2026-11-18 | DeepSeek 三角形子模板许可独立验收 | 暂不通过。平台形可误获满额子浪分，嵌套 E 腿关联被禁用。上轮两项修复和现有回归通过。 | [独立验收](2026-11-18-triangle-children-acceptance.md) |
| 2026-11-18 | P3-R01、P3-R02 及三角形 E 腿许可实现记录 | 实现自验通过。三角形许可的独立复验见上行。 | [修复与复验](2026-11-18-parent-subwave-fix.md) |
| 2026-11-18 | `0f56676` P3 独立验收 | 暂不通过：2 项 P2 误认证；171 项测试、21 组快照、10 个前缀及六项历史修复复验通过 | [验收报告](2026-11-18-parent-subwave-acceptance.md)、[独立证据](2026-11-18-parent-subwave-acceptance-evidence.json) |
| 2026-11-18 | P3 父级上下文与子浪规则认证实现记录 | 实现自验：171 项测试；21 组冻结样本；5 组长历史 40 个展示候选 | [P3 记录](2026-11-18-p3-parent-subwave.md)、[长历史 v3 证据](../validation_20261118_long_history_v3.json) |
| 2026-11-18 | P0/P1/P2 长历史、宽搜索、模板与子浪存在性 | 140 项测试、5 组长历史快照验证通过 | [长历史验证摘要](../validation_long_history_20261118.json) |
| 2026-11-18 | `9e00a89` 引擎扩展独立验收 | 初次验收未通过，复现 6 项边界问题 | [验收报告](2026-11-18-engine-acceptance.md)、[复现证据](2026-11-18-engine-evidence.json) |
| 2026-11-18 | ENG-R01 至 ENG-R06 修复 | 六项反例、168 项修复时测试（P3 后 171 项）、21 组快照与 10 个长历史前缀复验通过 | [修复记录](2026-11-18-engine-fix.md)、[复验证据](2026-11-18-engine-fix-evidence.json) |
| 2026-11-17 | DeepSeek 对首次提交 `9c6db8f` 之后的 ext5 工作区修改 | 初次有条件通过；失败记录保留，后续修复已复验 | [初次验收](2026-11-17-deepseek-ext5-acceptance.md)、[初次证据](2026-11-17-deepseek-ext5-evidence.json) |
| 2026-11-17 | R01/R02 修复与 D01 评分版本区分 | 修复复验通过，详见各项证据 | [修复记录](2026-11-17-deepseek-ext5-fixed.md)、[复验证据](2026-11-17-deepseek-ext5-fixed-evidence.json) |
| 2026-11-17 | 沪深京指数日线/周线接入 | 122 项测试、7 组快照验证通过 | [指数验证摘要](../validation_cn_index_20261117.json) |
| 2026-11-17 | `b63c505` 中国指数接入独立复验 | 通过；122 项测试、7 组冻结样本、21 个前缀及沪深京 3 次真实下载通过 | [独立验收](2026-11-17-cn-index-acceptance.md)、[离线证据](2026-11-17-cn-index-evidence.json)、[在线证据](2026-11-17-cn-index-live-evidence.json) |
