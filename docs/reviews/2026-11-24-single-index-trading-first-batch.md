# 单指数交易指导首批实现与复验

本批实现 TD-01、TD-02、TD-05 的本地基础设施。任务与后续依赖见[实施方案](../SINGLE_INDEX_TRADING_IMPLEMENTATION_PLAN.md)。

| 编号 | 实际行为 | 本地复验 |
| --- | --- | --- |
| TD-01 | 日线、周线和盘中实时简报在完整观察期尚未开始时发行。校验后的 `issuance.json` 绑定简报清单及行情时点。跨首期开盘时，CLI 用原分析输入和新时点重算，只有成功发行进入历史。前向评分核对回执及实际发行时点。 | 注入交易时钟验证等于首期开盘、迟一微秒、发布后发现越界、重做并只登记一次。用日线、周线和同日盘中发行夹具复验。旧报告读取和结算走兼容用例。 |
| TD-02 | 原始和分析 K 线按时间窗共同过滤，有效可用时间取两侧较晚者。中间格点保留为缺口，完整会话和周线检查子项。明确在截止之后才获知的复权因子不会进入输入。 | 用 Hypothesis 排列不同的可用延迟，独立计算应进入前缀的根数、格点、时间和原始 bar ID。延迟补到后可恢复。 |
| TD-05 | 支持矩阵读取测试回执。研究汇总另列。模型适用状态通过指定简报及注册表核对，涵盖标的、行情、时点、配置和事件。 | 复现只有 `APPROVED` 字符串的汇总，适用模型数为零。核对有效/过期测试回执、失败退出码，以及真实注册表在批准、撤回、错 feed 和文件篡改时的表现。 |

隔离缓存重跑科创50日线与沪深300腾讯 5m，分别得到 `OK`、`UNCLEAR`，两份均为 `FROZEN_DATA_REPLAY` 及 `UNCALIBRATED`。和修改前同快照记录逐项比对，分析摘要、卡片、观察窗、跨周期背景及概率状态均一致。回放输入见[日线冻结清单](../validation_20261118_cn_index_v3.json)及[盘中数据验收](2026-11-23-intraday-acceptance.md)。

从仓库根目录运行。行情样本可按对应清单装入临时缓存后再执行 CLI。

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
.venv/bin/python scripts/support_matrix.py --run-tests --test-receipts reports/support-tests
```

最终全量回归 610 项通过，记录 1970 条 `exchange_calendars` 依赖弃用告警。Ruff、依赖检查和差异检查通过。在临时目录运行回执生成入口，三个测试文件分别通过 13、8、8 项。正式 CLI 读回三份回执，将测试状态显示为 `PASS`。另构建 wheel，抽查四个改动模块均与工作区源码逐字一致。

真实行情首次接收、通知送达和交易时段连续发行仍需新样本。回执目前对应本地报告可读时刻。外部分发回执和实际成交时点随相应交易功能接入。下一批为 TD-03 的短线和波段条件预案。
