# 单指数 D 批代码审查修复

| 审查范围 | 修复与回归 |
| --- | --- |
| 封存与发行网格 | 封存日线分析和指导配置的摘要，以及登记盘中研究时的源与触发策略。收到不同配置发行会记录 `POLICY_MISMATCH`。空目录保留完整的漏发网格，有无效报告的目录仍须核验。 |
| 跨分段与配对区间 | 全分段和分段单跑使用同样的次段截止，跨段标签保留在网格并剔除。用循环日期块覆盖留出尾段，五会话样本末日从 104 降到 90 时区间下界小于零。 |
| 真实触发与可追溯数据 | 指数后续出现未知缺口时保留已有触发，随访标为待核。登记其他 15m feed 后复核两完整会话的价格基准、所引用的原始 K 线及可见时间。前向指数顺逆向点数使用已核验日线。 |
| ETF 入场、退出与估值 | 从首笔买入开始计算 ETF 会话数，独立保留指数提前退出和标签截止。ETF 追价越限终止入场。估值使用该会话收盘的现金、持仓和价格，并保存实际收盘价收到时间。回归中周二买入未进入周一的 10,000 净值。 |
| 压力、跟踪与公司行为 | 延迟情景分别跳过一次可买及可卖开盘，零可见量不计为可成交。迟到旧 bar 的波动计入持仓路径，跟踪止损只使用该 bar 开始前已可见的高点。拆分与其它行动同一时间且顺序不明确时拒绝数据。 |

复现：

```bash
.venv/bin/python -m pytest tests/test_index_execution.py tests/test_index_context.py -q
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

使用合成回归核对成交时序和前向凭据。冻结 ETF 行情仍沿用 D 批本地归档，行情缺少开盘盘口的状态继续保留。 [原 D 批验收](2026-11-25-single-index-trading-batch-d.md)
