# 小时线与分钟线独立验收

验收代码为 `90dd640`，对照基线为 `v0.6.0`（`ca1dc3b`）。本轮结论为暂缓发布。盘中五根观察窗、主要命令和多市场离线读取已能运行。正常取数仍可能接受陈旧行情或错标的，新浪 1 分钟源块重组破坏了集合竞价时隙，结算和前向核验也有可复现的问题。 [实施方案](../INTRADAY_IMPLEMENTATION_PLAN.md) · [开发交付记录](2026-11-23-intraday-acceptance.md)

后续修复和实测见[盘中独立验收缺陷修复](2026-11-24-intraday-repair.md)。

## 已独立验证的路径

- 全量 pytest **568 项通过**。Ruff lint、格式、`pip check` 和 `git diff v0.6.0..HEAD --check` 通过。
- 从 `git archive 90dd640` 在临时目录构建并独立安装 wheel。将四份真实原始快照复制到隔离缓存，实跑 `brief SYMBOL --offline --snapshot ID --format json --json`。四份摘要的 manifest 可由 `read_brief_report` 校验，观察窗均为 `1.1 / BARS / 5`，包含五个时隙。

| 标的 | 周期和来源 | 冻结快照 ID | 结果 |
| --- | --- | --- | --- |
| 贵州茅台 | `60m / sina` | `00d67c91c914c115fe3a6dbd33c3ecfa4ff945133056164e0ef5cdb591e83118` | `UNCLEAR / AVAILABLE` |
| AAPL | `5m / sip` | `b7336d55d525ccf61cd8092243ba227fc2385dae78d0ec877704af06f418b976` | `OK / AVAILABLE` |
| 沪深 300 | `1m / tencent-cn-index` | `4c0622fd057eaee4c3651dbac397f4423e4bcc66876b37f4e99b5e3ee58ddb0b` | `OK / AVAILABLE` |
| 标普 500 指数 | `1m / yahoo-index` | `8b265e9791f95549d653d6a22368c9fcbfbc2bd09b25bb0fdccbb4e87138c52e` | `OK / AVAILABLE` |

用同一 AAPL SIP 5m 快照派生 60m 父周期，在增强模式下重跑父子浪映射。独立检查了这次输出中 10 条 `VERIFIED` 关系的父、子端点价格，全部一致。以上真实数据证据来自已有冻结快照。交易时段的连续更新、全量真实组合以及 Tushare、Massive 的账户权限仍待实测。

## 待修缺陷与复验标准

### IR-01 · P1　同交易日缺最后一根行情仍生成正常简报

- 独立复现。用 AAPL 合成 5m 序列模拟供应商在同一天少返回最后一根。设截止为最后完整 bar 的计划结束后 20 分钟，应取到结束于 `18:35Z` 的 bar。经 `fetch_bundle → build_brief` 仍使用 `18:30Z`，简报状态为 `OK`，冻结决策的五根观察窗仍为 `AVAILABLE`。Yahoo 假响应也在请求 `10:30 ET`、最新已可用区间应从 `10:05 ET` 开始时接受了只覆盖到 `09:40 ET` 开始的行情。
- 原因。Yahoo、腾讯与源块重组先把预期时隙截到数据尾部，workflow 再只核对最后 bar 的交易日期。这会把落后的结果缓存到当前请求下。复验应覆盖冷取数、源块和整包缓存，按日历及 feed 延迟核对最后一个**应已可用的区间**；缺尾返回陈旧或数据不足。完整尾段和显式读取旧冻结快照仍应可用。 [Yahoo](../../src/elliottwaver/data/yahoo_provider.py#L205) · [腾讯](../../src/elliottwaver/data/tencent_provider.py#L129) · [源块](../../src/elliottwaver/data/source_cache.py#L413) · [workflow](../../src/elliottwaver/data/workflow.py#L342)

### IR-02 · P1　Yahoo 错标的响应被标记为已核验

- 独立复现。用 `FakeYahoo` 返回请求的时间戳，但把日线和分钟响应的 `meta.symbol` 改为 `MSFT`。请求 `AAPL` 时仍返回 `canonical_symbol=AAPL`、`identity_verified=True`，通过 workflow。
- 复验标准。分别核验日线与盘中响应的标的身份，正确处理已明确支持的类别股与指数映射。错标的、缺身份或不同系列的混合响应在进入源块前失败。正确的原生指数和类别股响应应通过。 [Yahoo 响应和快照身份](../../src/elliottwaver/data/yahoo_provider.py#L153) · [标记位置](../../src/elliottwaver/data/yahoo_provider.py#L258) · [源块写入](../../src/elliottwaver/data/workflow.py#L294)

### IR-03 · P1　Massive 认证错误会把 Key 带入错误文本

- 独立复现。用带虚构占位符的 HTTP 403 响应调用适配器，`raise_for_status()` 产生的错误字符串含请求 URL 和该占位符。批量处理路径会将异常文本写入标的诊断。复验全程使用虚构值。
- 复验标准。将权限错误转换为只含安全诊断信息的项目异常。模拟 401、403 和超时，检查终端错误、批量 JSON 和失败记录中都没有凭证；退出码与其他数据源的认证失败一致。 [Massive 请求](../../src/elliottwaver/data/massive_provider.py#L34) · [批量异常处理](../../src/elliottwaver/cli/main.py#L577)

### IR-04 · P1　源块重组把新浪已知竞价空档当作缺口

- 独立复现。复制交付中的真实 600519.SH 1m 快照 `adfdc3a50d3c59c71c402c8adee14c60ab8556d5bd4a515ff69742fa4f2ff412`。按新浪原生解析器的已知窗口契约恢复预期时隙，去掉每个会话 14:57、14:58 起始的两个空档。K 线价格、数量和时间原样保留。经 `SourceBlockStore.store_bundle → assemble` 后未知缺口从 0 增到 18，简报从 `OK` 变为 `INSUFFICIENT_DATA`。交付中保存的该份重组快照本身已有 18 个缺口。
- 复验标准。源块、重组及盘中派生时按实际来源的时隙规则保留已知空档，保留真实的收盘 bar。上述快照恢复为零未知缺口。再删除一根本应存在的连续竞价 bar，严格模式应报告数据不足。 [新浪原生规则](../../src/elliottwaver/data/sina_provider.py#L288) · [重组入口](../../src/elliottwaver/data/source_cache.py#L413)

### IR-05 · P2　盘中缓存跨日重新请求整个历史区间

- 独立复现。用计数供应商按 AAPL 5m 的 `2024-01-04 15:00 ET`、`2024-01-05 13:00 ET` 连续请求，设置 `history_years=1`。请求分别从 `2023-01-03`、`2023-01-04` 开始，源缓存产生两个身份。日历版本含滚动请求区间的会话摘要，源级身份也包含日线背景末日。已有块的过期条件还把早于当前请求的所有会话都视为应重取。
- 复验标准。用同一个计数供应商推进到下一根和下一交易日。重用相容的已完成源块，只请求尚未完成、需复查或缺失的部分。日历或日线背景改变时，派生摘要按对应身份重算。单独验证旧时点、当前时点交错运行。 [源缓存身份](../../src/elliottwaver/data/workflow.py#L183) · [续取条件](../../src/elliottwaver/data/workflow.py#L242) · [日历版本](../../src/elliottwaver/data/calendar.py#L75)

### IR-06 · P2　同一会话的不完整刷新覆盖已校验的历史

- 独立复现。在隔离源缓存中保存一个会话的 49 根 bar，再以同一源身份写入该会话只含最后 2 根的响应。当前 revision 成为 2 根，原先 47 根从最新版消失。现有回归只覆盖刷新返回日期早于目标的情形。
- 复验标准。按预期区间和 raw/adjusted 配对确认新分块覆盖。复验同日缺头、缺中段、空响应、重复页，以及后续完整修订。缺证据时保留上一份已校验的有效区间，并记录边界。不能证明各段价格相容时返回诊断。 [源块写入](../../src/elliottwaver/data/source_cache.py#L256)

### IR-07 · P2　同日尚未到期的盘中观测被标为缺失

- 独立复现。决策截止为 `19:35Z`，首个未来区间为 `19:35–19:40Z`，观察快照只含 `19:35Z` 刚完成的上一根。经真实 `intraday_outlook_window → evaluate_outlook_event` 返回 `INCOMPLETE_DATA`。此时首个未来区间还没结束，应为 `RIGHT_CENSORED`。
- 复验标准。用期望区间的结束与实际可用时刻判断右删失，避免退回按日期比较。覆盖尚未结束、已结束仍未可用、已被后续行情跨过的未知缺口三个边界。 [区间状态判定](../../src/elliottwaver/evaluation/outcomes.py#L59) · [盘中覆盖判断](../../src/elliottwaver/evaluation/outcomes.py#L366)

### IR-08 · P2　盘中复权触发记录引用了换算后的 bar ID

- 独立复现。构造拆股换算比例为 2，未来 5m K 线最高价 55，发行坐标上的推进条件为 `HIGH > 105`。求值正确得到 `CONFIRMED_FIRST`，但记录 ID 为 `7cf60c912a482d61638be0b6`，源 bar ID 为 `bc2be0eb979d38a81522c186`。后者才可在快照中定位。
- 复验标准。计算使用发行坐标，触发审计使用原始来源的 ID 与区间。对一份拆股后的快照同时核对结算记录和评估 Parquet；两个触发 ID 均可在对应快照查到。 [价格换算](../../src/elliottwaver/evaluation/outcomes.py#L327) · [触发记录](../../src/elliottwaver/evaluation/outcomes.py#L476)

### IR-09 · P2　前向回执仍按日期去重

- 独立复现。生成并发布两份 manifest 可验的 AAPL 5m `LIVE` 简报，最后一根分别结束于当天 `18:25Z`、`18:30Z`。`live_reports` 报错 `Multiple live decisions for ('AAPL', '5m', '2024-01-05')`。评估读取端同样按交易日期取回执。
- 复验标准。使用精确盘中 bar 时刻索引和匹配回执，同日多个时点分别使用其实际发行的观察窗。同一个决策的冲突回执仍应被拒绝，日周回执继续完成原有流程。 [回执索引](../../src/elliottwaver/evaluation/forward.py#L21) · [评估匹配](../../src/elliottwaver/evaluation/decisions.py#L833)

### IR-10 · P2　仓库现有安装包与交付记录不匹配

- 独立复现。开发交付文档列出 `077e6f708a14ad03d4b2542ed937bc041ff9ca758b5a41b6210e2294d8f5eb6d`。当前仓库 `dist/elliottwaver-0.6.0-py3-none-any.whl` 实际 SHA256 为 `c8b1bdd473651117f85d48313a023bf330cc1d0bcaa6ff75b736eed84957e561`，包内缺少 Tencent、Yahoo、源块缓存等本轮新增模块。
- 复验标准。提供从最终发布提交构建的版本化安装包，随记录给出实际产物路径和 SHA256。在独立安装目录核对包路径、版本及盘中入口。本轮从 `90dd640` 临时重构建并安装已成功，上表的实测使用该临时包。 [开发交付记录](2026-11-23-intraday-acceptance.md#6-验证命令与结果)

## 复验范围

| 工作项 | 独立结论 |
| --- | --- |
| IN-00、IN-04 | 盘中 5B 替代旧周线窗口通过。新鲜行情的发行边界受 IR-01 和 IR-04 约束。 |
| IN-01、IN-02 | 样本路径可读。行情完整性、身份、密钥错误处理及源块增量需按 IR-01 至 IR-06 复验。 |
| IN-03 | 精确锚点回归及 AAPL SIP 重叠样本通过。 |
| IN-05 | 同日历史查询、基础冻结观察窗和同根案例通过。IR-07 至 IR-09 仍影响结算证据和前向评估。 |
| IN-06、IN-07 | 四个真实冻结样本通过。发行包一致性、实盘连续更新和授权源验证仍待完成。 |
