# 美股日用版实施与验收记录

基线为 `v0.1.0`（`60a4800`），实现与测试提交为 `5ae45a5`。本轮按[美股日用版实施与验收计划](../US_EQUITY_IMPLEMENTATION_PLAN.md)推进 US-01 至 US-05，范围是 Alpaca 已完成美股日线、完整周线和规则摘要，覆盖上市股票与 ETF。当前环境已配置 Alpaca 凭证；本文记录真实 SIP/IEX 样本、离线证据和仍存在的范围限制。

## 结论

- **US-01 通过（离线/冻结响应）**：资产端点返回的代码必须与 `route_symbol()` 的规范 provider 代码一致；`AAPL` 错返 `MSFT` 时在 K 线请求前终止。`BRK-B` 与 `BRK.B` 使用同一资产。分页、尾页缺失、raw/all 对齐、无时区时间、交易日历外记录、重复 token、403、429 和畸形 JSON 均有独立测试。
- **US-02 通过（冻结响应/日历）**：纽约会话、DST、假期、提前收盘、完整周、周中请求、内部缺日、raw/all 周线复权和 2:1 价格桥接均有测试。原生日线可用时刻在 2024-03-08 为 `05:20Z`，2024-03-11 为 `04:20Z`。
- **US-03 通过（离线入口）**：`brief`、`analyze`、`data fetch`、`data inspect`、`watchlist` 的 JSON/终端/Markdown/HTML 均显示 feed、复权、行情日期、分析截止和模式。离线 `brief` 与直接 `build_brief`、离线 `analyze` 与直接 `AnalysisService` 的结果摘要一致。SIP/IEX 使用不同缓存身份。
- **US-04 通过（共享引擎/回放/结算）**：同一决策日的前缀分析与全量分析得到相同候选、条件和目标；`replay` manifest 记录源快照和 `FROZEN_DATA_REPLAY`；AAPL 真实报告已跑出目标先达和失效先达结算路径。真实同根双触及样本未出现，歧义路径明确标为待定，通用回归保留。
- **US-05 通过（真实 SIP 小样本）**：AAPL、BRK.B、SPY 的 SIP 快照和 HTML 摘要已生成，AAPL 的 IEX 快照也已生成；BRK-B 与 BRK.B 返回同一 Alpaca 资产 ID。原生日 OHLC、SPY 完整周 OHLC、SPY 分红复权、AAPL 候选规则和 SPY `UNCLEAR` 案例均已手算/读回核对。范围仅限这三只标的，未外推为全市场验证。

## 验证结果

从仓库根目录执行：

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
.venv/bin/eliot doctor --market US
```

结果：

- `341 passed`；Ruff 检查通过，98 个文件格式通过；`pip check` 无损坏依赖。
- `doctor --market US` 退出 0，`alpaca_credentials_configured=true`，`network_smoke=NOT_RUN`。
- 测试包含新增的 `tests/test_us_equity.py` 36 项，覆盖资产身份、分页、时区、DST、假期、提前收盘、完整周、raw/all、缓存隔离、离线入口、自选、回放和价格桥接。

## US-01 资产身份与响应

修改 `src/eliotbot/data/alpaca_provider.py`：

- 资产响应必须包含非空 `id`、`class=us_equity`，且 `symbol` 经 `route_symbol()` 规范化后等于请求的 `provider_symbol`。
- 不一致时抛出 `ASSET_IDENTITY_MISMATCH`，错误信息只含请求代码、返回代码和错误类别；K 线请求次数为零。
- `BRK-B` 和 `BRK.B` 都规范化到 `BRK.B`，资产 ID 相同。
- `_pages` 将缺失尾页转为 `SourceError`，拒绝非字符串 token，继续拒绝重复 token。
- `convert` 显式拒绝无时区时间、非交易日记录和畸形 OHLC；raw/all 时间戳必须逐根对齐，否则 `ALIGNMENT_ERROR`。
- 目标会话缺失返回 `STALE_DATA`；重复时间戳冲突由 `normalize_bars` 拒绝。

对应测试：`test_alpaca_asset_symbol_mismatch_stops_before_bars`、`test_alpaca_share_class_aliases_resolve_to_one_asset`、`test_alpaca_empty_page_with_token_then_final_page`、`test_alpaca_missing_tail_page_is_a_source_error`、`test_alpaca_missing_target_session_is_stale`、`test_alpaca_raw_missing_a_day_is_alignment_error`、`test_alpaca_timestamp_requires_a_timezone`、`test_alpaca_rejects_a_bar_outside_the_exchange_calendar`、`test_alpaca_conflicting_duplicate_timestamp_is_rejected`。

## US-02 会话、完整周与价格坐标

- `bar_window()` 对美股使用纽约当地日界，原生日线可用时刻为次日 00:00 加 20 分钟缓冲；DST 测试通过。
- `parse_as_of("YYYY-MM-DD")` 对美股使用同一口径；2024-03-08 得到 `2024-03-09T05:20Z`，2024-03-11 得到 `2024-03-12T04:20Z`。
- 2024-03-29 休市、2024-11-29 13:00 ET 提前收盘、2024-07-03 半日市均由 XNYS 日历提供。
- 2024-03-25 至 03-28 的缩短周被视为完整周；当前未完成周不进入周线。周中请求使用上一个完整周；缺内部一日的周被排除。
- 周线先按供应商 `all` 分析价聚合，`raw_bars` 保留原始价。2:1 价格桥接测试通过，现金分红走同一价格桥接路径。

对应测试：`test_us_dst_daily_availability_is_local_next_midnight_plus_buffer`、`test_us_date_only_as_of_uses_local_next_midnight`、`test_us_holiday_and_early_close_are_calendar_facts`、`test_us_shortened_week_is_complete_but_current_partial_week_is_not`、`test_us_midweek_weekly_target_is_previous_complete_week`、`test_us_provider_weekly_uses_adjusted_bars_and_preserves_raw`、`test_us_week_with_an_internal_missing_session_is_not_complete`、`test_provider_price_scale_bridges_a_clean_split`。

## US-03 日用入口与行情来源

修改 `cli/main.py`、`cli/console_render.py`、`visualization/brief_formatter.py`、`visualization/report_formatter.py` 和 `visualization/chart_builder.py`：

- `brief` 的 JSON、Markdown、HTML 和终端显示 `feed`、`adjustment`、`market_asof`、`as_of`、`mode`。
- `analyze` 的终端、Markdown 和图表 HTML 显示同一组元信息。
- `data fetch` 输出快照 ID、feed、复权、行情截止、分析截止、最后可用时刻和模式；`data inspect` 增加最后可用时刻。
- `watchlist` 每条记录包含 feed、复权、周期和模式，表格增加来源列；单项失败不丢失其他结果。
- 离线入口拒绝不同 feed；同一快照的 `brief`/`analyze` 与直接服务调用结果摘要一致。
- SIP/IEX 使用不同缓存 key、快照 ID 和 feed 身份；历史跨 feed 沿用 `NOT_COMPARABLE`。

离线证据：

| 标的 | 快照 ID | 结果 |
| --- | --- | --- |
| `AAPL` 日线 | `2f0b79fb4ea78d8834d34626488ca3e6d75d51f4fb2defc1c73a401a96122e48` | `brief` 与 `analyze` 均为 `5d52c5531d9a…`，状态 OK |
| `SPY` 周线 | `caf6f0bb8d8ff25a1534dbebfe667fc7a16b48720020c65549b2e36b9f34a454` | `brief` 结果 `8ca7b9005a57…`，状态 OK |
| `BRK.B` 日线 | `1a98f5ad4df92daac2827998f0227290a0fde7a615413ddb5788bd46a7a76168` | `demo` 报告生成，状态 OK |

这些是 `synthetic` 演示快照，不是真实 Alpaca 行情，只用于验证入口、元信息、缓存和报告一致性。

## US-04 规则、回放与结算

- 同一冻结快照在决策日 T 的全量分析与物理删除未来行后的前缀分析，候选集合、结构和条件价一致。
- `replay` manifest 记录 `mode=FROZEN_DATA_REPLAY`、`source_snapshot_id`、引擎、配置、日期范围和报告数；逐条结果保留 `as_of` 和 `market_asof`。
- 结算沿用 `evaluation/outcomes.py`、`settlement.py` 和 `provider_price_scale`；无法确认价格桥接时返回 `PRICE_BASIS_UNVERIFIED`。
- N 个交易日按交易日历推进；缺中间日期不顺延；同一根 K 线同时触达目标和失效边界返回 `AMBIGUOUS_SAME_BAR`。这些沿用现有 A 股/通用回归，未修改波浪评分。

对应测试：`test_prefix_analysis_matches_full_analysis_at_the_same_decision`、`test_replay_manifest_records_source_and_frozen_mode`、`test_provider_price_scale_bridges_a_clean_split`，以及既有 `tests/test_replay_and_reports.py`、`tests/test_settlement.py`、`tests/test_engine.py`。

### 真实 AAPL 结算

使用真实 AAPL SIP 快照 `cff55390639d…d3b1d3`，在同一价格坐标 `ccd7e1dd6eec…` 上运行 `eliot settle`：

| 决策日 | 报告摘要 | 结果 |
| --- | --- | --- |
| 2026-03-02 | `605af79ffbcc…` | 两个候选 `TARGET_FIRST` 于 2026-03-13；两个 `CONFIRMED_FIRST` 于 2026-03-06；5D 收盘 `RANGE`，20D 收盘 `DOWN` |
| 2026-02-27 | `c1650273fd42…` | 三个候选 `INVALIDATED_FIRST` 于 2026-03-06；5D/20D 收盘 `DOWN` |
| 2026-10-01 | `16073c5f33c9…` | 5D/20D 收盘均 `SETTLED`，另有 `CONFIRMED_FIRST` 和 `EXPIRED` 目标路径 |

结算的 `settlement_snapshot_id` 与 `settlement_input_snapshot_id` 均为源快照，`price_coordinate_id` 与发行报告一致。真实样本中没有出现同一根 K 线同时触达目标和失效的案例，因此 `AMBIGUOUS_SAME_BAR` 真实路径标为**待定**；通用回归仍在 `tests/test_replay_and_reports.py`。明细见 `reports/us-acceptance/real-settlement-summary.json`。

## US-05 真实账户联调

### 真实快照

| 标的 | feed/周期 | 快照 ID | 根数 | 行情区间 | 状态/摘要 |
| --- | --- | --- | ---: | --- | --- |
| AAPL | SIP / 1d | `cff55390639d9397a6d5417974193d7cc1ae6b7ddc53a869bce59faee6d3b1d3` | 1257 | 2021-11-16 → 2026-11-18 | `brief`/`analyze` OK；结果 `cd277af91bdd…`；brief `17eec1dadc5e…` |
| BRK.B | SIP / 1d | `80964e37adbe614105f931688ffe2f70936b62987fecb6d7b466b2ba8f3f0023` | 1257 | 2021-11-16 → 2026-11-18 | `brief` OK；结果 `460dbf3369ca…` |
| SPY | SIP / 1d | `27ac145f8599cf679dbe830acdcda6f54fd60471a290f361a94700d023919659` | 1257 | 2021-11-16 → 2026-11-18 | `brief` `UNCLEAR`；结果 `780874dcec61…` |
| SPY | SIP / 1w | `aa6da70ee4b52d0874be024d9d92861c470e4037a0daac80b94794b70159b937` | 260 | 2021-11-26 → 2026-11-13 | `brief` OK；结果 `284d6b191b1a…` |
| AAPL | IEX / 1d | `2735ac1877953ebf9da085b6e54be117e77ef9ccc7e30fc80c9efadf850f7804` | 1257 | 2021-11-16 → 2026-11-18 | `brief` OK；结果 `269a63638a00…` |
| BRK-B 别名 | SIP / 1d | `38714baeb4f24d3d839174ece4d48664729b0b5f95d1159289805ae0ee22fa98` | 1257 | 2021-11-16 → 2026-11-18 | 与 `BRK.B` 同一资产 ID `alpaca:71bb19d0-6c5f-4881-9552-33224badd810` |

日线快照的 `last_available_at` 为 `2026-11-19T05:20:00Z`（纽约 2026-11-19 00:20），`market_asof=2026-11-18`；SPY 周线快照的 `last_available_at` 为 `2026-11-14T05:20:00Z`，`market_asof=2026-11-13`。真实报告位于 `reports/us-acceptance/real-*-brief/`，按仓库惯例不提交 Git。

### 手算与读回

- AAPL 2026-11-18 原生日：`open=351.94`、`high=356.42`、`low=351.4706`、`close=352.45`、`volume=34,378,520`；`raw` 与 `all` 收盘同为 352.45。
- SPY 2026-11-09 至 2026-11-13 完整周：`open=741.95`、`high=763.04`、`low=741.025`、`close=754.37`、`volume=206,674,566`。由 SPY 日线快照聚合的 OHLCV 与独立周线快照逐项一致。
- SPY 2025-11-20：`raw=652.53`、`all=645.53`，比例 `0.98927252`，证明供应商 `all` 已包含分红复权；周线和日线都使用同一 `provider_all` 坐标。
- AAPL 候选：`上涨推动 · W5_DEVELOPING`，质量 56.97、覆盖 0.85；条件为 `STAGE_EXIT < 338.04`、`CONFIRMATION > 355.11`，目标区 `355.31–361.20`、`367.17–374.32`、`381.57–385.41`；最新价 352.45，价格坐标 `ccd7e1dd6eec…`。
- SPY 日线 `brief` 返回 `UNCLEAR`，作为“结果不明确”案例保留，不强行给出通过结论。
- 5 份真实报告均用 `read_brief_report` 校验 manifest、文件哈希和 `brief_digest`；文件数分别为 7、7、7、5、7。
- `data fetch BRK-B` 与 `BRK.B` 的真实资产 ID 相同。期间修复了一个缓存命中 bug：分析截止后移时，缓存 bundle 的 `as_of` 会改变快照身份；现在会先保存新 bundle 再返回，避免出现无快照目录的 ID。

### 真实请求命令

```bash
.venv/bin/eliot data fetch AAPL --timeframe 1d --feed sip --config reports/us-acceptance/config.yaml
.venv/bin/eliot data fetch BRK.B --timeframe 1d --feed sip --config reports/us-acceptance/config.yaml
.venv/bin/eliot data fetch SPY --timeframe 1d --feed sip --config reports/us-acceptance/config.yaml
.venv/bin/eliot data fetch SPY --timeframe 1w --feed sip --config reports/us-acceptance/config.yaml
.venv/bin/eliot data fetch AAPL --timeframe 1d --feed iex --config reports/us-acceptance/config.yaml
.venv/bin/eliot brief AAPL --offline --snapshot <AAPL-SIP> --feed sip --config reports/us-acceptance/config.yaml
.venv/bin/eliot analyze AAPL --offline --snapshot <AAPL-SIP> --feed sip --config reports/us-acceptance/config.yaml
```

SIP 与 IEX 的 AAPL 快照不同：SIP 收盘 352.45、成交量 34,378,520；IEX 收盘 352.30、成交量 983,549。两者缓存、快照 ID、价格坐标和报告身份均分离。

### 范围限制

- 真实样本只覆盖 AAPL、BRK.B、SPY 三只标的；没有外推到全市场或所有 ETF。
- 未验证盘中分钟线的真实 SIP/IEX 联调；本轮验收聚焦 `1d/1w`。
- 没有对真实账户下单、账户查询或交易执行做测试，这些不在分析程序范围内。

## 交付状态

- US-01 至 US-04：离线/冻结响应验收通过。
- US-05：真实 SIP 小样本（AAPL、BRK.B、SPY）和 AAPL IEX 样本验收通过；范围见上。
- 整体美股日用版发布状态为**首批小样本真实验收通过**；实现与测试提交 `5ae45a5`，验收记录与证据提交 `3113156`。

机器可读证据见[验收证据](2026-11-20-us-equity-acceptance-evidence.json)。

## 独立复验后的修复

- 读取 Alpaca 快照时复核规范代码、供应商代码和身份状态。旧缓存里的错标的会触发重新取数，显式打开同一快照会收到身份错误。
- 缓存复用时，派生快照采用请求的精确截止时间，并保留缓存索引指向原始取数。若缓存包含请求时刻尚不可用的 K 线或背景数据，则按请求截止重新取数。
- 新生成的默认 Plotly 图表在标题下显示数据模式及完整决策截止时间。使用原有的 AAPL SIP 日线和 SPY SIP 周线快照可离线核对。
- Alpaca 盘中缓存按最后可用盘中 K 线和最近可用原生日线区分。派生快照按原始取数和精确截止索引，重复请求可直接读回，并沿用原始取数的有效期。
- Plotly 报告的输出路径包含渲染版本，单周期报告的路径另外包含运行模式。同一业务结果下，重新生成的历史图表显示该次运行的实际模式。
