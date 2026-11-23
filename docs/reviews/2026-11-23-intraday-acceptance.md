# 小时线与分钟线验收记录

日期：2026-11-23。回归基线：`v0.6.0`（`ca1dc3b`）。本轮实现提交：`8f8bb7d`（`Add intraday brief, history, settlement and evaluation support`）；后续 `e439632`（文档与旧快照兼容回归）、`fc3f844`（Massive 美股指数）和源块缓存提交。

范围：沪深个股、沪深主要指数、美股个股和 ETF 的 `1m/5m/15m/30m/60m`，`1h` 归一为 `60m`。原生美股指数按 IN-07 独立处理。本文区分“已离线验证”“已用真实快照验证”“因凭证或接口未取得而阻塞”。

## 1. 任务状态

| 任务 | 状态 | 证据与边界 |
| --- | --- | --- |
| IN-00 盘中简报停止周线观察窗 | 完成 | 60m/5m 合成与真实快照均输出 `version=1.1`、`horizon_unit=BARS`、`horizon=5`，不再出现 `2W` 事件。旧盘中 `1.0` 周线观察窗在结算时返回 `SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH`。 |
| IN-01 行情适配、会话和聚合 | 部分完成 | 新浪/`sina-index` 和 Alpaca SIP/IEX 路径保留；新增 Tushare `stock_mins`/`idx_mins` 适配器、CN 盘中 feed 配置、覆盖范围和来源字段。Tushare 只用假响应验证，真实账户权限未取得。CN 指数盘中默认仍拒绝，配置 `cn_index_intraday_feed: tushare-cn-index` 后才路由。 |
| IN-02 历史覆盖与增量缓存 | 完成（盘中源块） | 新增 `SourceBlockStore`，按资产、feed、源周期、session profile、复权、parser、日历版本和日线背景结束日缓存每个交易日的源块；保留不可变修订。冷取数、热命中、尾段刷新、跨周期共享 5m 源块和精确截止零请求均有计数假供应商测试。整段权限失败保留、源级锁和日/周源块仍未覆盖。 |
| IN-03 精确锚点、多周期背景和逐腿证据 | 完成（盘中腿映射） | 支持带时区的精确 `--anchor`；固定配对 `1m→5m`、`5m→60m`、`15m/30m→60m`、`60m→1d` 已接入 `resample_bundle` 和简报背景；`hierarchical_v1` 允许盘中结构计算；新增 `engine/intraday_link.py`，按精确 K 线区间把父结构腿映射到子结构，复用 `match_interval`、子模板许可、深度和共享预算。合成 5m/60m 关系测试通过；长区间重叠的真实逐腿证据待补。 |
| IN-04 盘中简报与冻结观察窗 | 完成 | 发行后尚未开始的 5 根主周期 K 线；跨午休、周末和休市；最后一根 K 线结束/可用时刻；市场当地时间；`1h`/`60m` 同路径；`brief --timeframe auto` 对盘中快照自动采用快照周期。 |
| IN-05 同日历史、结算和评估 | 完成 | SQLite 增加周期/feed/复权/最后 K 线字段和索引；同日按最后 K 线排序；5B 事件注册；盘中观察窗解析器与结算/评估共用；同根目标/失效、未知缺口、右删失、拆股换算和未来修订测试通过。 |
| IN-06 自动化、真实行情、性能和发布 | 部分完成 | 全量 553 项测试、Ruff、格式、`pip check`、`git diff --check` 和 wheel 构建通过。真实 CN 个股 1m/5m/15m/30m/60m 与美股 NVDA 60m SIP 简报已核对；1m 因数据不足未发行，KSTR 5m SIP 也因数据不足未发行；CN 指数、Tushare 真实权限、Massive 真实权限、IEX 全周期和真实冷/热取数未完成；合成简报 p50/p95 已记录。 |
| IN-07 原生美股指数 | 代码完成，真实权限阻塞 | 已接入 Massive `I:` 指数聚合接口、`SPX`/`NDX` 路由、`us_index_feed: massive-index` 和 `MASSIVE_API_KEY`；假响应验证 5m 原生指数、无成交量证据和 workflow 路由。真实 Massive 账户凭证未取得，未提交真实指数验收。 |

## 2. 改动文件

- 盘中模型与简报：`src/elliottwaver/outlook.py`、`schemas/outlook.py`、`schemas/brief.py`、`brief.py`、`visualization/brief_text.py`、`visualization/brief_formatter.py`、`cli/console_render.py`、`cli/main.py`。
- 行情与会话：`data/tushare_provider.py`（新增）、`data/source_cache.py`（新增）、`data/sina_provider.py`、`data/alpaca_provider.py`、`data/workflow.py`、`data/periods.py`、`schemas/market.py`、`config.py`、`config.example.yaml`、`config.advanced.example.yaml`、`.env.example`。
- 锚点与引擎：`engine/span_selector.py`、`engine/matcher.py`、`engine/intraday_link.py`（新增）、`service.py`、`timeframes.py`、`schemas/wave.py`。
- 历史、结算与评估：`history.py`、`settlement.py`、`evaluation/events.py`、`evaluation/outcomes.py`、`evaluation/decisions.py`、`forecast.py`。
- 测试：`tests/test_intraday.py`（新增）、`tests/test_tushare_provider.py`（新增）、`tests/test_massive_provider.py`（新增）、`tests/test_source_cache.py`（新增）、`tests/test_settlement.py`（支持矩阵行数）。
- 文档：`README.md`、`docs/USER_GUIDE.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/ROADMAP.md`、`docs/reviews/README.md`、本记录。

## 3. IN-00 复现与修复

复现命令（合成快照）：

```bash
.venv/bin/elliottwaver demo --symbol 600519.SH --timeframe 1h
.venv/bin/elliottwaver brief 600519.SH --offline --snapshot 3d6e3333e4bc247e42ff3d54395997e38ff2779570268182ec27e5fe473226f1 --timeframe 1h --format json --json
.venv/bin/elliottwaver demo --symbol AAPL --timeframe 5m
.venv/bin/elliottwaver brief AAPL --offline --snapshot e6aabf086e433524098397d2955cfe342930f336320fc7d4436a97fa9ac3c4d4 --timeframe 5m --format json --json
```

预期：盘中 `outlook.version=1.1`、`horizon_unit=BARS`、`horizon=5`，事件 ID 为 `ADVANCE_FIRST_{TF}_5B_V1` 和 `OUTLOOK_TARGET_FIRST_{TF}_5B_V1`，不含 `2W`。

实测：

- CN 60m：`AVAILABLE`，5 个时隙，首个 `2024-04-09T06:00:00Z`，事件 `ADVANCE_FIRST_60M_5B_V1`、`OUTLOOK_TARGET_FIRST_60M_5B_V1`。
- US 5m：`AVAILABLE`，5 个时隙均在 `2024-01-05`，事件 `ADVANCE_FIRST_5M_5B_V1`、`OUTLOOK_TARGET_FIRST_5M_5B_V1`。
- 旧盘中 `1.0` 周线观察窗调用 `event_specs` 抛出 `SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH: 旧观察窗与主周期不匹配`。

## 4. 真实行情证据

### 4.1 沪深个股（新浪）

使用工作区已有的真实快照，缓存目录 `.cache/live-smoke`：

| 品种 | 周期 | feed | 快照 ID | 最后 K 线 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 600519.SH | 1m | sina | `adfdc3a50d3c59c71c402c8adee14c60ab8556d5bd4a515ff69742fa4f2ff412` | 2026-11-16 15:00 CST | `INSUFFICIENT_DATA`，5B 观察窗 `UNAVAILABLE`；未发行事件 |
| 600519.SH | 5m | sina | `eb270f1681277487b446b23f0487a8f5c9ac120bc817b844f08cfbc25cf42a29` | 2026-11-16 15:00 CST | `OK`，5B 观察窗，事件 `ADVANCE_FIRST_5M_5B_V1`、`OUTLOOK_TARGET_FIRST_5M_5B_V1` |
| 600519.SH | 15m | sina | `6f116b759b14021bb1fd45b4ace0ed94770cec78712a6ab87b883bcfabb27b9c` | 2026-11-16 15:00 CST | `OK`，5B 观察窗，事件 `ADVANCE_FIRST_15M_5B_V1`、`OUTLOOK_TARGET_FIRST_15M_5B_V1` |
| 600519.SH | 30m | sina | `fe40715ff916efb5174f2964e36aab010fd5801d63e1e4ed08e10a62d5de15f7` | 2026-11-16 15:00 CST | `OK`，5B 观察窗，事件 `ADVANCE_FIRST_30M_5B_V1`、`OUTLOOK_TARGET_FIRST_30M_5B_V1` |
| 600519.SH | 60m | sina | `00d67c91c914c115fe3a6dbd33c3ecfa4ff945133056164e0ef5cdb591e83118` | 2026-11-16 15:00 CST | `UNCLEAR`，5B 观察窗，事件 `ADVANCE_FIRST_60M_5B_V1`、`OUTLOOK_TARGET_FIRST_60M_5B_V1` |

复现方式：使用 `cache.directory: .cache/live-smoke` 的临时配置，运行：

```bash
.venv/bin/elliottwaver brief 600519.SH --offline \
  --snapshot 00d67c91c914c115fe3a6dbd33c3ecfa4ff945133056164e0ef5cdb591e83118 \
  --timeframe 60m --config <live-smoke-config.yaml> --json
.venv/bin/elliottwaver brief 600519.SH --offline \
  --snapshot eb270f1681277487b446b23f0487a8f5c9ac120bc817b844f08cfbc25cf42a29 \
  --timeframe 5m --config <live-smoke-config.yaml> --json
```

### 4.2 美股个股 / ETF（Alpaca）

使用工作区已有的真实 SIP 快照，缓存目录 `.cache/eliotbot`：

| 品种 | 周期 | feed | 快照 ID | 最后 K 线 | 结果 |
| --- | --- | --- | --- | --- | --- |
| NVDA | 60m | sip | `a994efbaf82a5e98cf41e224d7e64f0cba7824f268d52f0dae95f77005b19725` | 2026-11-20 16:00 ET | `OK`，5B 观察窗，事件 `ADVANCE_FIRST_60M_5B_V1`、`OUTLOOK_TARGET_FIRST_60M_5B_V1` |
| KSTR | 5m | sip | `c013a72635bea06eb20f56cf40abe594b19eae97df477afc8095db58f9dbe36e` | 2026-11-19 16:00 ET | `INSUFFICIENT_DATA`，仍输出 5B 观察窗状态；不发行事件 |

KSTR 5m 的真实快照未覆盖足够完整的历史/缺口，按方案继续完成可独立验证的部分，未把它解释为通过。IEX 真实样本本轮未取得；SIP/IEX 分离仍由既有冻结响应和入口测试覆盖。

### 4.3 沪深指数与 Tushare

- `data/tushare_provider.py` 已实现 `stock_mins`、`idx_mins` 和 `adj_factor` 的请求、时间标签按 bar 结束解析、8000 行/日期片段接口约定、覆盖范围和来源字段。
- 假响应测试验证 `000300.SH` 60m 四根窗口、`600519.SH` 60m 基准日复权和 `fetch_bundle` 路由。
- 真实 Tushare 账户未提供 `TUSHARE_TOKEN` 或权限回执，未提交真实指数/个股分钟证据。未配置 `cn_index_intraday_feed` 时，CN 指数盘中仍返回 `INDEX_INTRADAY_UNSUPPORTED`。

### 4.4 原生美股指数

IN-07 代码完成、真实权限阻塞：已按 Massive 官方 `GET /v2/aggs/ticker/I:{indicesTicker}/range/{multiplier}/{timespan}/{from}/{to}` 接入 `SPX`/`NDX`，指数无成交量时保留 `volume_comparable=False`。假响应验证了 5m 会话过滤、日线背景、`I:` 身份和 workflow 路由。真实 `MASSIVE_API_KEY` 和指数计划回执未取得，未用 ETF 或合成行情冒充原生指数。

## 5. A-01 至 A-07 独立预期与实测

| 用例 | 独立预期 | 实测 |
| --- | --- | --- |
| A-01 沪深午休/集合竞价 | 60m 四根：09:30–10:30、10:30–11:30、13:00–14:00、14:00–15:00；1m 稀疏竞价时隙不补造；15:00 收盘取实际成交 | `tests/test_timeframes.py` 通过；真实 600519 60m 观察窗跨午休正确；1m 稀疏规则测试通过 |
| A-02 美股 DST/提前收盘/稀疏成交 | 夏冬 UTC 偏移不同；提前收盘最后 30 分钟保留；缺一根子 bar 时粗周期窗口缺失；IEX 稀疏保留证据状态 | `tests/test_timeframes.py`、`test_alpaca_contract.py`、`test_sources.py` 通过；NVDA SIP 60m 真实简报通过；KSTR 5m 标记数据不足 |
| A-03 复权与源身份 | 2:1 拆股换回发行坐标命中原价；OHLC 不共用比例待核；源 bar ID 存在；股票/指数、SIP/IEX 分开 | 既有拆股/复权/源身份测试通过；新增 provenance 覆盖字段；Tushare 复权假响应测试通过；真实指数权限未取得 |
| A-04 精确边界与规则 | 同日两个锚点范围正确；确认前后逐时比较；浪 2/浪 3/浪 4/浪 5/三角形 E 浪和亚型正反例 | 精确时间锚点测试通过；既有规则/亚型测试通过；`hierarchical_v1` 5m 结构测试通过；盘中父子腿映射仍为部分完成 |
| A-05 盘中简报与结算 | 手算 5 根窗口；同根目标/失效、确认在先、只到目标、未知首根、最后一根尚不可用、跨午休/次日；结算与评估同事件 | `test_intraday.py` 覆盖 5 根窗口、同根歧义、缺失首根、结算事件、评估 5B 行；结算与评估共用 `intraday_outlook_window` |
| A-06 前缀与历史 | 物理截断到决策时刻；篡改未来一根不影响原决策；同日两份逆序写入仍按行情先后 | 盘中 replay 前缀测试通过；同日历史逆序写入测试通过；未来修订由既有结算 revision 测试覆盖 |
| A-07 兼容与性能 | 读取旧快照/旧简报/旧 SQLite；并发坏标的隔离；冷/热取数、分析、自选的 p50/p95 | 旧真实快照兼容读取测试通过；旧 SQLite 迁移与并发自选既有测试通过。合成 `build_brief` 5 次：600519 60m p50 0.254s / p95 0.552s；AAPL 5m p50 0.422s / p95 0.488s。真实冷/热取数请求次数未测 |

### 5.1 IN-02 源块缓存独立证据

`tests/test_source_cache.py` 使用带调用计数的假供应商和真实 5m 合成快照：

| 用例 | 预期 | 实测 |
| --- | --- | --- |
| 冷取数 → 热命中 | 第一次 1 次请求，第二次 0 次新增请求 | 两次调用计数均为 1（第二次未增加） |
| 尾段刷新 | 只从最后缓存交易日重取；同一 session 新增 revision | 第二次调用 start 等于最后 session；revision 数 +1；最新 close 使用修订值 |
| 跨周期共享 | 先取 5m，再取 15m，供应商调用不增加 | 5m 冷取 1 次；15m 请求仍为 1 次，输出 15m 聚合 bars |
| 精确截止 | 请求更早 as_of，供应商零新增请求 | 调用计数保持 1，返回截止时刻前的 bars |
| 后移截止 | 缓存块 cutoff 早于新 as_of，只重取目标 session | 新增 1 次请求，返回更晚的可用 bar |

## 6. 验证命令与结果

```text
.venv/bin/python -m pytest tests -q              553 passed
.venv/bin/ruff check src tests scripts           PASS
.venv/bin/ruff format --check src tests scripts  PASS
.venv/bin/python -m pip check                    No broken requirements found
git diff --check                                 PASS
```

Wheel：

```text
elliottwaver-0.6.0-py3-none-any.whl
sha256 8b99764552a8da00fd5edee9f94ffdd75075a457f6f4aa2462b1f6c4fedc98c0
```

## 7. 未完成项与原因

1. **IN-02 剩余边界**：盘中源块、尾段刷新、修订和跨周期共享已完成；源级锁仍复用整包缓存锁，供应商中途失败时只保留已完成源块，不保存部分页面级覆盖原因；日线和周线仍走整包快照缓存。
2. **IN-03 剩余边界**：盘中父子腿映射已实现并通过合成关系测试；尚未取得足够长、父子区间重叠的真实快照来逐腿确认 VERIFIED 比例。日周 `timeframe_link` 保持原路径。
3. **IN-06 真实覆盖**：CN 指数、Tushare 真实权限、IEX 真实样本、BRK.B/SPY 全五周期和真实冷/热取数请求次数未完成。原因：账户权限/凭证和样本时间限制。合成简报 p50/p95 已记录。
4. **IN-07 真实权限**：Massive 指数适配器和假响应测试已完成；真实 `MASSIVE_API_KEY`/指数计划回执未取得，真实 SPX/NDX 逐周期证据待补。

这些限制不影响已经通过的离线 5B 观察窗、同日历史、结算、评估和旧快照兼容路径；后续取得权限后按同一记录追加真实组合。
