# 小时线与分钟线验收记录

日期：2026-11-23。回归基线：`v0.6.0`（`ca1dc3b`）。本轮实现提交：`8f8bb7d`（`Add intraday brief, history, settlement and evaluation support`）；后续 `e439632`（文档与旧快照兼容回归）、`fc3f844`（Massive 美股指数）。

范围：沪深个股、沪深主要指数、美股个股和 ETF 的 `1m/5m/15m/30m/60m`，`1h` 归一为 `60m`。原生美股指数按 IN-07 独立处理。本文区分“已离线验证”“已用真实快照验证”“因凭证或接口未取得而阻塞”。

## 1. 任务状态

| 任务 | 状态 | 证据与边界 |
| --- | --- | --- |
| IN-00 盘中简报停止周线观察窗 | 完成 | 60m/5m 合成与真实快照均输出 `version=1.1`、`horizon_unit=BARS`、`horizon=5`，不再出现 `2W` 事件。旧盘中 `1.0` 周线观察窗在结算时返回 `SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH`。 |
| IN-01 行情适配、会话和聚合 | 部分完成 | 新浪/`sina-index` 和 Alpaca SIP/IEX 路径保留；新增 Tushare `stock_mins`/`idx_mins` 适配器、CN 盘中 feed 配置、覆盖范围和来源字段。Tushare 只用假响应验证，真实账户权限未取得。CN 指数盘中默认仍拒绝，配置 `cn_index_intraday_feed: tushare-cn-index` 后才路由。 |
| IN-02 历史覆盖与增量缓存 | 部分完成 | 缓存键加入资产类型、session profile、源周期、聚合规则和所有盘中市场的日线背景结束日；快照/`data inspect`/`data fetch` 增加请求范围、实际首尾、覆盖原因、缺口数和价格坐标。整段源块缓存、尾段增量合并和冲突修订存储尚未实现，仍按整包快照缓存。 |
| IN-03 精确锚点、多周期背景和逐腿证据 | 部分完成 | 支持带时区的精确 `--anchor`；固定配对 `1m→5m`、`5m→60m`、`15m/30m→60m`、`60m→1d` 已接入 `resample_bundle` 和简报背景；`hierarchical_v1` 已允许盘中结构计算。日周专用的 `timeframe_link` 尚未泛化为盘中父子腿映射，盘中高级关联仍标记为不可用。 |
| IN-04 盘中简报与冻结观察窗 | 完成 | 发行后尚未开始的 5 根主周期 K 线；跨午休、周末和休市；最后一根 K 线结束/可用时刻；市场当地时间；`1h`/`60m` 同路径；`brief --timeframe auto` 对盘中快照自动采用快照周期。 |
| IN-05 同日历史、结算和评估 | 完成 | SQLite 增加周期/feed/复权/最后 K 线字段和索引；同日按最后 K 线排序；5B 事件注册；盘中观察窗解析器与结算/评估共用；同根目标/失效、未知缺口、右删失、拆股换算和未来修订测试通过。 |
| IN-06 自动化、真实行情、性能和发布 | 部分完成 | 全量 547 项测试、Ruff、格式、`pip check`、`git diff --check` 和 wheel 构建通过。真实 CN 个股 60m/5m 与美股 NVDA 60m SIP 简报通过；KSTR 5m SIP 因数据不足未发行；CN 指数、Tushare 真实权限、Massive 真实权限和真实冷/热取数未完成；合成简报 p50/p95 已记录。 |
| IN-07 原生美股指数 | 代码完成，真实权限阻塞 | 已接入 Massive `I:` 指数聚合接口、`SPX`/`NDX` 路由、`us_index_feed: massive-index` 和 `MASSIVE_API_KEY`；假响应验证 5m 原生指数、无成交量证据和 workflow 路由。真实 Massive 账户凭证未取得，未提交真实指数验收。 |

## 2. 改动文件

- 盘中模型与简报：`src/elliottwaver/outlook.py`、`schemas/outlook.py`、`schemas/brief.py`、`brief.py`、`visualization/brief_text.py`、`visualization/brief_formatter.py`、`cli/console_render.py`、`cli/main.py`。
- 行情与会话：`data/tushare_provider.py`（新增）、`data/sina_provider.py`、`data/alpaca_provider.py`、`data/workflow.py`、`data/periods.py`、`schemas/market.py`、`config.py`、`config.example.yaml`、`config.advanced.example.yaml`、`.env.example`。
- 锚点与引擎：`engine/span_selector.py`、`engine/matcher.py`、`service.py`、`timeframes.py`、`schemas/wave.py`。
- 历史、结算与评估：`history.py`、`settlement.py`、`evaluation/events.py`、`evaluation/outcomes.py`、`evaluation/decisions.py`、`forecast.py`。
- 测试：`tests/test_intraday.py`（新增）、`tests/test_tushare_provider.py`（新增）、`tests/test_settlement.py`（支持矩阵行数）。
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
| 600519.SH | 60m | sina | `00d67c91c914c115fe3a6dbd33c3ecfa4ff945133056164e0ef5cdb591e83118` | 2026-11-16 15:00 CST | `UNCLEAR`，5B 观察窗，事件 `ADVANCE_FIRST_60M_5B_V1`、`OUTLOOK_TARGET_FIRST_60M_5B_V1` |
| 600519.SH | 5m | sina | `eb270f1681277487b446b23f0487a8f5c9ac120bc817b844f08cfbc25cf42a29` | 2026-11-16 15:00 CST | `OK`，5B 观察窗，事件 `ADVANCE_FIRST_5M_5B_V1`、`OUTLOOK_TARGET_FIRST_5M_5B_V1` |

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

## 6. 验证命令与结果

```text
.venv/bin/python -m pytest tests -q              547 passed
.venv/bin/ruff check src tests scripts           PASS
.venv/bin/ruff format --check src tests scripts  PASS
.venv/bin/python -m pip check                    No broken requirements found
git diff --check                                 PASS
```

Wheel：

```text
elliottwaver-0.6.0-py3-none-any.whl
sha256 4607aeb67b4c67ba139ddd929b1fe5e09382fd8986e82011b98054ce3db5c12e
```

## 7. 未完成项与原因

1. **IN-02 源块缓存/尾段增量**：当前仍是整包快照缓存。已加入覆盖范围、日线背景结束日和精确截止缓存键，但没有按交易日保存可复用源块、没有 `--refresh` 只取尾段、没有冲突修订表。原因：需要新增源块 schema、锁粒度和跨周期合并，本轮优先保证简报/结算/评估链路正确。
2. **IN-03 盘中父子腿映射**：`hierarchical_v1` 已能计算盘中结构，`resample_bundle` 已提供固定背景配对；`timeframe_link` 仍按日/周 ISO 周分组，未泛化为盘中区间包含映射。盘中高级关联暂不声称通过。
3. **IN-06 真实覆盖**：CN 指数、Tushare 真实权限、IEX 真实样本、BRK.B/SPY 全五周期和真实冷/热取数请求次数未完成。原因：账户权限/凭证和样本时间限制。合成简报 p50/p95 已记录。
4. **IN-07 真实权限**：Massive 指数适配器和假响应测试已完成；真实 `MASSIVE_API_KEY`/指数计划回执未取得，真实 SPX/NDX 逐周期证据待补。

这些限制不影响已经通过的离线 5B 观察窗、同日历史、结算、评估和旧快照兼容路径；后续取得权限后按同一记录追加真实组合。
