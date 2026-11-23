# 小时线与分钟线验收记录

日期：2026-11-23。回归基线：`v0.6.0`（`ca1dc3b`）。本轮实现提交：`8f8bb7d`（`Add intraday brief, history, settlement and evaluation support`）；后续 `e439632`（文档与旧快照兼容回归）、`fc3f844`（Massive 美股指数）、`ab47f42`（源块缓存与尾段刷新）、`c38103a`（盘中父子腿映射）和 `383a7bf`（源块刷新加固）。

范围：沪深个股、沪深主要指数、美股个股和 ETF 的 `1m/5m/15m/30m/60m`，`1h` 归一为 `60m`。原生美股指数按 IN-07 独立处理。本文区分“已离线验证”“已用真实快照验证”“因凭证或接口未取得而阻塞”。

## 1. 任务状态

| 任务 | 状态 | 证据与边界 |
| --- | --- | --- |
| IN-00 盘中简报停止周线观察窗 | 完成 | 60m/5m 合成与真实快照均输出 `version=1.1`、`horizon_unit=BARS`、`horizon=5`，不再出现 `2W` 事件。旧盘中 `1.0` 周线观察窗在结算时返回 `SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH`。 |
| IN-01 行情适配、会话和聚合 | 部分完成 | 新浪/`sina-index` 和 Alpaca SIP/IEX 路径保留；新增 Tushare `stock_mins`/`idx_mins` 适配器、CN 盘中 feed 配置、覆盖范围和来源字段。Tushare 只用假响应验证，真实账户权限未取得。CN 指数盘中默认仍拒绝，配置 `cn_index_intraday_feed: tushare-cn-index` 后才路由。 |
| IN-02 历史覆盖与增量缓存 | 完成（盘中源块） | 新增 `SourceBlockStore`，按资产、feed、源周期、session profile、复权、parser、日历版本和日线背景结束日缓存每个交易日的源块；保留不可变修订；源级文件锁串行化同一源区间；供应商失败和部分覆盖写入 `source_failures`，已校验块保留。冷取数、热命中、尾段刷新、跨周期共享 5m 源块、精确截止零请求和失败保留均有计数假供应商测试。日/周源块已在 §5.4 明确排除。 |
| IN-03 精确锚点、多周期背景和逐腿证据 | 完成（含真实重叠样本） | 支持带时区的精确 `--anchor`；固定配对 `1m→5m`、`5m→60m`、`15m/30m→60m`、`60m→1d` 已接入 `resample_bundle` 和简报背景；`hierarchical_v1` 允许盘中结构计算；新增 `engine/intraday_link.py`，按精确 K 线区间把父结构腿映射到子结构，复用 `match_interval`、子模板许可、深度和共享预算。AAPL SIP 5m/60m 真实重叠样本得到 43 条关系，其中 10 条 `VERIFIED`、33 条 `PROVISIONAL`。 |
| IN-04 盘中简报与冻结观察窗 | 完成 | 发行后尚未开始的 5 根主周期 K 线；跨午休、周末和休市；最后一根 K 线结束/可用时刻；市场当地时间；`1h`/`60m` 同路径；`brief --timeframe auto` 对盘中快照自动采用快照周期。 |
| IN-05 同日历史、结算和评估 | 完成 | SQLite 增加周期/feed/复权/最后 K 线字段和索引；同日按最后 K 线排序；5B 事件注册；盘中观察窗解析器与结算/评估共用；同根目标/失效、未知缺口、右删失、拆股换算和未来修订测试通过。 |
| IN-06 自动化、真实行情、性能和发布 | 完成（免费源覆盖） | 全量 568 项测试、Ruff、格式、`pip check`、`git diff --check` 和 wheel 构建通过。真实 CN 个股/指数、AAPL/BRK.B/SPY SIP/IEX、SPX/NDX Yahoo 和完全免费无 Key 组合均已核对；BaoStock 5m/60m 历史回补通过；BRK.B IEX 与 Yahoo 15m/30m/60m 缺口按限制记录。Tushare 与 Massive 授权源仍因凭证阻塞。 |
| IN-07 原生美股指数 | 完成（Yahoo 免费源；Massive 授权源待凭证） | 支持 `SPX`/`NDX` 等原生指数路由；Massive `I:` 适配器和假响应测试已完成；另接入 Yahoo 免费 chart 源，真实 SPX/NDX 1m/5m/15m/30m/60m 已取得。Yahoo 15m/30m/60m 存在少量缺失，验收时显式 `--allow-gaps`；Massive 真实权限仍未取得。 |

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
| AAPL | 5m | iex | `8b3b30874041c6a1d0369f60d34909a63a60efe500faf967827b63c4439f40ed` | 2026-11-20 15:40 ET | `OK`，7016 根，5B 观察窗，事件 `ADVANCE_FIRST_5M_5B_V1`、`OUTLOOK_TARGET_FIRST_5M_5B_V1` |
| KSTR | 5m | sip | `c013a72635bea06eb20f56cf40abe594b19eae97df477afc8095db58f9dbe36e` | 2026-11-19 16:00 ET | `INSUFFICIENT_DATA`，仍输出 5B 观察窗状态；不发行事件 |

KSTR 5m 的真实快照未覆盖足够完整的历史/缺口，按方案继续完成可独立验证的部分，未把它解释为通过。IEX 真实样本本轮未取得；SIP/IEX 分离仍由既有冻结响应和入口测试覆盖。

### 4.2.1 AAPL / BRK.B / SPY SIP 与 IEX 矩阵

2026-11-20 收盘后，使用真实 Alpaca 快照运行 `brief --offline`。所有条目均使用 `1h` 对应的 `60m` 规范周期；`15m/30m/60m` 由同一 5m 源块聚合。

| 品种 | feed | 周期 | 快照 ID | bars | 缺口 | brief 状态 | 5B 观察窗 |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| AAPL | sip | 15m | `eead93baf5cceacb7029c406afb8524d50750cacf1dd9c3cd886eab48f6ac11a` | 2338 | 0 | OK | AVAILABLE |
| AAPL | sip | 30m | `d272b9558e828eda089e820898ea6edb934e23f0850d703c82139cbc26fea900` | 1169 | 0 | OK | AVAILABLE |
| AAPL | sip | 60m | `88d57fbbf7e213306709a2aa5d874cb399ca69d8551fc0050c71da914fa1e2fc` | 629 | 0 | OK | AVAILABLE |
| AAPL | iex | 15m | `23ebf10218bb026d70f69be0b0def7e91cb5a8b982326c7f46fe9ac89adc4679` | 2338 | 0 | OK | AVAILABLE |
| AAPL | iex | 30m | `a2ca098929e0eca967c3fcaf4cc0a1c1edc68f61c70bbf9a76ee1e33a0bfdc04` | 1169 | 0 | OK | AVAILABLE |
| AAPL | iex | 1m | `85a0b592cd25c2fe376cd35556f802061b4ed163c7c03e6a40e09c5629c115f3` | 35077 | 3 | UNCLEAR | AVAILABLE |
| AAPL | iex | 60m | `a50a52f3fb6921d38be6e2b42694a1353c7ad4f8a9763b948d4aef6a7f6567e9` | 629 | 0 | OK | AVAILABLE |
| BRK.B | sip | 15m | `69e74e6cc55b0781d0df83f9917a0eba5b50c82227f6c8e39eb17d2430733da2` | 2338 | 0 | OK | AVAILABLE |
| BRK.B | sip | 30m | `5a39a61a802600659e318cda2bd9dde9aa24eed0219e260645a4c96012321a12` | 1169 | 0 | UNCLEAR | AVAILABLE |
| BRK.B | sip | 60m | `c19a529ec658375d976860af509803a78c6514bfd74b037bd48051919417e7d4` | 629 | 0 | UNCLEAR | UNAVAILABLE |
| BRK.B | iex | 1m | `215c6a0aab5a7795726b0c4992db0edc6f0a65d68e90527264ef4d2519d6536a` | 28162 | 6918 | INSUFFICIENT_DATA | UNAVAILABLE |
| BRK.B | iex | 15m | `7aafec3a2190e529abe093d426c1135b2f0235c7525fa826288cc4a55ee58a8a` | 2298 | 40 | INSUFFICIENT_DATA | UNAVAILABLE |
| BRK.B | iex | 30m | `c88216e4cc8539d6a195abde976fb7dbe4617bb4eb9dd381f0af5ae0fc4c14a6` | 1132 | 37 | INSUFFICIENT_DATA | UNAVAILABLE |
| BRK.B | iex | 60m | `6582509bfef44d56fb881c759d649a452c86d7c1168675e5e98c80a6ba653951` | 594 | 35 | INSUFFICIENT_DATA | UNAVAILABLE |
| SPY | sip | 15m | `37408d4b61cb54761f762e1c3986097376a278f06052669edc1fea76a8504519` | 2338 | 0 | OK | AVAILABLE |
| SPY | sip | 30m | `30da329facf0d254f66c72cf530da497403453177b712ec0d1bef7195c8e1085` | 1169 | 0 | OK | AVAILABLE |
| SPY | sip | 60m | `ad9227c7dc28a0f3e95a242f4839223b5e4ada7434f46eba7e510a4ec62e1d96` | 629 | 0 | OK | AVAILABLE |
| SPY | iex | 1m | `069f5c7b1a4abd8e7d75d75de463c3e9485295f09310f330485048b140a6e56c` | 35060 | 20 | OK | AVAILABLE |
| SPY | iex | 15m | `cd6344ad40a0affb95834eb0db7f289e30d37c0c0d7c0f06341fc8b4518e2886` | 2338 | 0 | OK | AVAILABLE |
| SPY | iex | 30m | `e64f14d035115c114567622fe80fa9663a1dca171b9038926a1fa8f36a251cce` | 1169 | 0 | OK | AVAILABLE |
| SPY | iex | 60m | `cead3eb3b3921d3d16b5e278a1407342544b212c244a6282bd2f628a7034e805` | 629 | 0 | OK | AVAILABLE |

BRK.B IEX 的 1m 有 6918 个未知缺口，15m/30m/60m 分别为 40/37/35，分析状态为 `INSUFFICIENT_DATA`；5B 观察窗仍按 `version=1.1`、`horizon_unit=BARS` 输出但状态为 `UNAVAILABLE`，未发行事件。AAPL 1m 有 3 个缺口但仍为 `UNCLEAR/AVAILABLE`；SPY 1m 有 20 个缺口，仍为 `OK/AVAILABLE`。BRK.B SIP 60m 的候选状态为 `UNCLEAR`，没有可发行卡片，因此观察窗 `UNAVAILABLE`。这些是数据质量和候选质量结果，不是接口失败。

### 4.3.1 沪深指数 Tencent 免费源矩阵

使用 `data.cn_index_intraday_feed: tencent-cn-index`，无需 Token。2026-11-20 15:05 CST 截止：

| 指数 | 周期 | 快照 ID | bars | 缺口 | brief | 5B 观察窗 |
| --- | --- | --- | ---: | ---: | --- | --- |
| 000300.SH | 1m | `4c0622fd057eaee4c3651dbac397f4423e4bcc66876b37f4e99b5e3ee58ddb0b` | 5040 | 0 | OK | AVAILABLE |
| 000300.SH | 5m | `fc7183f28b4cbf6f50982da615e9e351f0a60c4cb45ae3289c3c2a5c0b66f200` | 5904 | 0 | UNCLEAR | UNAVAILABLE |
| 000300.SH | 15m | `fe3ad3941eababba2a283bc86a644cc2909a8513460ba50690132dc0ddcda5a7` | 1968 | 0 | OK | AVAILABLE |
| 000300.SH | 30m | `d80bd0bb6e6b81d8c689cbd6daafc47248dcc3d4a8ed3b49b6a607f0a4ba53a1` | 1928 | 0 | OK | AVAILABLE |
| 000300.SH | 60m | `fe2b8f5f1fbb7e02acf13dd11eaec0f08eda1f38ee91bc2ef7f3890670127ac9` | 964 | 0 | OK | AVAILABLE |
| 000001.SH | 1m | `110e058b4bf5c4b3d211888c9ae9e787ce10fdf6d70056935c056c3c2ed833ab` | 5040 | 0 | OK | AVAILABLE |
| 000001.SH | 5m | `2479fad2322f1e959b248f090c4d4c8412ae53bb29045ea8b0b94ecfad2d2709` | 5904 | 0 | OK | AVAILABLE |
| 000001.SH | 15m | `6206a3fcc82e06a190580a6e948562f6c88a6e155595e14b7677cda7cd08f62d` | 1968 | 0 | OK | AVAILABLE |
| 000001.SH | 30m | `bdbf6934626a3a119501a28f2c202b605dcc5e2835da67ce8c5b8548319706e8` | 1928 | 0 | OK | AVAILABLE |
| 000001.SH | 60m | `870d8a4f7dd1a9b987796eea39c661d146355a13391060497241802f8d0b923d` | 964 | 0 | UNCLEAR | UNAVAILABLE |
| 399001.SZ | 1m | `b6416cb83a06e15f3ea65718ddb5634c7b8578c2bb39c00b594da49b6f13522e` | 5038 | 2 | OK | AVAILABLE |
| 399001.SZ | 5m | `7da037973f2bd705e5ec96e987988add3bbb9ef79c7a9a5bfe6a5b8c72e2187b` | 5904 | 0 | OK | AVAILABLE |
| 399001.SZ | 15m | `e24ce7b659dfd4917370e799ec0112a4eb3eab6febbee1ebc03105726ee55567` | 1968 | 0 | OK | AVAILABLE |
| 399001.SZ | 30m | `c74fc75b173c1fecd564881c27837788d666a0b8c13c1f20cbbbf7fd3be67466` | 1928 | 0 | UNCLEAR | AVAILABLE |
| 399001.SZ | 60m | `4d2dbb9f962bbb79a31d41849b3b8dedc2b00fa5f26db6bf2763cebab4b4f7ad` | 964 | 0 | OK | AVAILABLE |

Tencent 是免费公开接口，无 Token，但无 SLA；成交量单位未认证，`volume_comparable=False`。399001.SZ 1m 有 2 个未知缺口，仍为 `OK/AVAILABLE`。

### 4.3 沪深指数与 Tushare

- `data/tushare_provider.py` 已实现 `stock_mins`、`idx_mins` 和 `adj_factor` 的请求、时间标签按 bar 结束解析、8000 行/日期片段接口约定、覆盖范围和来源字段。
- 假响应测试验证 `000300.SH` 60m 四根窗口、`600519.SH` 60m 基准日复权和 `fetch_bundle` 路由。
- 真实 Tushare 账户未提供 `TUSHARE_TOKEN` 或权限回执，未提交真实指数/个股分钟证据。未配置 `cn_index_intraday_feed` 时，CN 指数盘中仍返回 `INDEX_INTRADAY_UNSUPPORTED`。

### 4.4.1 SPX / NDX Yahoo 免费源矩阵

使用 `data.us_index_feed: yahoo-index`，无需 Token。2026-11-20 21:30 UTC 截止：

| 指数 | 周期 | 快照 ID | bars | 缺口 | brief | 5B 观察窗 |
| --- | --- | --- | ---: | ---: | --- | --- |
| SPX | 1m | `8b265e9791f95549d653d6a22368c9fcbfbc2bd09b25bb0fdccbb4e87138c52e` | 2730 | 0 | OK | AVAILABLE |
| SPX | 5m | `3a13f4d2aeeb641a7cd3718af5490267a4c1464afb54237f5cc42e8a4679cb47` | 4661 | 19 | OK | AVAILABLE |
| SPX | 15m | `c60241486915f386c6095f0ccc475f27fa2fcecd11c50e844c9bfe2e522c6aee` | 1554 | 6 | OK（`--allow-gaps`） | AVAILABLE |
| SPX | 30m | `fba6c2dfade481705a2fe711cd10aadb49b00f2b1182d73e071a69bf3f7bd31a` | 778 | 2 | OK（`--allow-gaps`） | AVAILABLE |
| SPX | 60m | `c0e8d8956abfa5a75be6c6989a7bc7ef5ff0a12eae7f00061f4ee7a8631f82af` | 419 | 1 | OK（`--allow-gaps`） | AVAILABLE |
| NDX | 1m | `dcc1a1bda9f73b05cbca7a77dd077ee17545e99a5f0c61f527eb4767ae7d63c7` | 2730 | 0 | OK | AVAILABLE |
| NDX | 5m | `4aef49694c1a80a82f3f86961922818d188c81a21c23cfa82e1f0ac4f23921da` | 4661 | 19 | OK | AVAILABLE |
| NDX | 15m | `a70d73f21f74ac5a6da1801620538fda5bacc71392489ab4ac4075c9c19fdc52` | 1554 | 6 | OK（`--allow-gaps`） | AVAILABLE |
| NDX | 30m | `6361c5648773241af9bae1d5f2798f6c33d3c02a77b17b39ec2913b9086fa2fa` | 778 | 2 | OK（`--allow-gaps`） | AVAILABLE |
| NDX | 60m | `72b8182895d463a86896ccfc1e8f0bf89bd70aa6a3daccab6d80718fb43b7435` | 419 | 1 | OK（`--allow-gaps`） | AVAILABLE |

Yahoo 是免费公开接口，无 Token，但非官方、无 SLA；1m 只覆盖最近约 7 天，5m/15m/30m/60m 约 60 天。指数成交量不作为可比证据。15m/30m/60m 有少量缺失，默认会判 `INSUFFICIENT_DATA`，验收时显式使用 `--allow-gaps` 并在报告中保留 `ALLOWED_GAPS`。Massive 授权源仍待凭证。

### 4.4 原生美股指数

IN-07 代码完成、真实权限阻塞：已按 Massive 官方 `GET /v2/aggs/ticker/I:{indicesTicker}/range/{multiplier}/{timespan}/{from}/{to}` 接入 `SPX`/`NDX`，指数无成交量时保留 `volume_comparable=False`。假响应验证了 5m 会话过滤、日线背景、`I:` 身份和 workflow 路由。真实 `MASSIVE_API_KEY` 和指数计划回执未取得，未用 ETF 或合成行情冒充原生指数。

### 4.5 完全免费、无需 API Key 组合

配置：[config.free.example.yaml](../../config.free.example.yaml)。CN 个股/指数使用 Tencent，US 个股/ETF/指数使用 Yahoo，均为免费公开接口，无需 Token。

#### CN 个股 Tencent

| 周期 | 快照 ID | bars | 缺口 | brief |
| --- | --- | ---: | ---: | --- |
| 1m | `9e7e4ba42cebf929e440774a25152fd58aa99fede0867be3c8b45842af3fc29e` | 5040 | 0 | OK / AVAILABLE |
| 5m | `0a72b044cf1f2f374fa17622f7320cf634445a5fef6e691fd8be440efbe16922` | 5904 | 0 | OK / AVAILABLE |
| 15m | `32412e77b5d613aceb8c3ff58d0554648249c8e063179e719277027c0c32433e` | 1968 | 0 | UNCLEAR / UNAVAILABLE |
| 30m | `ecd28d56183b50d5b0f45d3892979c45763c2a94f76377f7bf253a7b84483b3a` | 1928 | 0 | OK / AVAILABLE |
| 60m | `793863b097bc2b7b1dfe8deacf39b7d21e44c307167293b288106cb568e42c3f` | 964 | 0 | OK / AVAILABLE |

#### CN 个股 BaoStock 历史回补

| 周期 | 快照 ID | bars | 缺口 | 结果 |
| --- | --- | ---: | ---: | --- |
| 5m | `e4586cf3edb0a38835f97a31b8b755e42bef80ec9e791e9389a3d199b90520cc` | 58368 | 0 | QFQ，历史回补 |
| 60m | `b7037dd54b7af7c7f1962b0048fd4649f60f2ff2386f83212462bdfb208bfa05` | 4864 | 0 | QFQ，历史回补 |

BaoStock 没有 1m，也不提供指数分钟线；请求 1m 返回 `BAOSTOCK_1M_UNSUPPORTED`。它作为 `5m/15m/30m/60m` 的长历史回补源，不是实时源。

#### US 个股/ETF Yahoo

| 品种 | 周期 | 快照 ID | bars | 缺口 | brief |
| --- | --- | --- | ---: | ---: | --- |
| AAPL | 1m | `89b110ada105175dd439c5d2eeafd8c0c41d0deb164780f4e235c6b2e216b09b` | 2730 | 0 | OK |
| AAPL | 5m | `82315f44db683b3fb1976c2cb5035bee27fdcc7aa10524cdb55babdbe25ba5ec` | 4661 | 19 | OK |
| AAPL | 15m | `d471090fa40aee85d42eadd4ab738712492c7d7d6d9e89f83edba82ac8aabb77` | 1554 | 6 | OK（`--allow-gaps`） |
| AAPL | 30m | `a2274e6c218ff84be1a8d3968f21e7a0eb948ee7b5ef6ce94dd712b04590f895` | 778 | 2 | OK（`--allow-gaps`） |
| AAPL | 60m | `4f1151d6f2040d70cfdcd0558e045449f226a7564c90cdc4a4fe7fdbafe9e289` | 419 | 1 | OK（`--allow-gaps`） |
| SPY | 1m | `81faa02d592e0d145479ef2bb9e38fbcb499e5a0caee40140a896bc244bea12b` | 2730 | 0 | OK |
| SPY | 5m | `14fdf7021ae7e63f3c5f7714750046722bedfa870ef1a5eb7eee1cfaf8da3f6f` | 4661 | 19 | OK |
| SPY | 15m | `5bfdfa52ae68ea8c8e082f8cb5d656a45c4dbef5abf21a73f9972884d849b0e0` | 1554 | 6 | OK（`--allow-gaps`） |
| SPY | 30m | `f8f0635bb1482d28b96416cc6a18071ad71f9da26ed4c02b127f7c8c3f48fe91` | 777 | 3 | OK（`--allow-gaps`） |
| SPY | 60m | `acf4ed2a10780074b204ccbf5e7320e3f449fd8c96a3547dc5159a1d8b27d73d` | 418 | 2 | OK（`--allow-gaps`） |

Yahoo 15m/30m/60m 的缺口需要显式 `--allow-gaps`；报告保留 `ALLOWED_GAPS`，不把缺口当作完整数据。Yahoo 免费源无 SLA，1m 只覆盖最近约 7 天。

可靠性处理：Tencent/Yahoo 请求使用有界重试和 `Retry-After` 退避；源块按 session 原子提交并保留 revision；失败和部分覆盖写入 `source_failures`；所有源分别记录 feed、parser、覆盖范围、缺口和价格口径。

## 5. A-01 至 A-07 独立预期与实测

| 用例 | 独立预期 | 实测 |
| --- | --- | --- |
| A-01 沪深午休/集合竞价 | 60m 四根：09:30–10:30、10:30–11:30、13:00–14:00、14:00–15:00；1m 稀疏竞价时隙不补造；15:00 收盘取实际成交 | `tests/test_timeframes.py` 通过；真实 600519 60m 观察窗跨午休正确；1m 稀疏规则测试通过 |
| A-02 美股 DST/提前收盘/稀疏成交 | 夏冬 UTC 偏移不同；提前收盘最后 30 分钟保留；缺一根子 bar 时粗周期窗口缺失；IEX 稀疏保留证据状态 | `tests/test_timeframes.py`、`test_alpaca_contract.py`、`test_sources.py` 通过；NVDA SIP 60m 真实简报通过；KSTR 5m 标记数据不足 |
| A-03 复权与源身份 | 2:1 拆股换回发行坐标命中原价；OHLC 不共用比例待核；源 bar ID 存在；股票/指数、SIP/IEX 分开 | 既有拆股/复权/源身份测试通过；新增 provenance 覆盖字段；Tushare 复权假响应测试通过；真实指数权限未取得 |
| A-04 精确边界与规则 | 同日两个锚点范围正确；确认前后逐时比较；浪 2/浪 3/浪 4/浪 5/三角形 E 浪和亚型正反例 | 精确时间锚点测试通过；既有规则/亚型测试通过；`hierarchical_v1` 5m 结构测试通过；盘中父子腿映射仍为部分完成 |
| A-05 盘中简报与结算 | 手算 5 根窗口；同根目标/失效、确认在先、只到目标、未知首根、最后一根尚不可用、跨午休/次日；结算与评估同事件 | `test_intraday.py` 覆盖 5 根窗口、同根歧义、缺失首根、结算事件、评估 5B 行；结算与评估共用 `intraday_outlook_window` |
| A-06 前缀与历史 | 物理截断到决策时刻；篡改未来一根不影响原决策；同日两份逆序写入仍按行情先后 | 盘中 replay 前缀测试通过；同日历史逆序写入测试通过；未来修订由既有结算 revision 测试覆盖 |
| A-07 兼容与性能 | 读取旧快照/旧简报/旧 SQLite；并发坏标的隔离；冷/热取数、分析、自选的 p50/p95 | 旧真实快照兼容读取测试通过；旧 SQLite 迁移与并发自选既有测试通过。真实 AAPL IEX 5m 冷/热 provider 调用 1/1，AAPL IEX 5m→60m 共享源块调用 1/1，NVDA SIP 60m 调用 1/1。合成 `build_brief` 5 次：600519 60m p50 0.254s / p95 0.552s；AAPL 5m p50 0.422s / p95 0.488s |

### 5.1 IN-02 源块缓存独立证据

`tests/test_source_cache.py` 使用带调用计数的假供应商和真实 5m 合成快照：

| 用例 | 预期 | 实测 |
| --- | --- | --- |
| 冷取数 → 热命中 | 第一次 1 次请求，第二次 0 次新增请求 | 两次调用计数均为 1（第二次未增加） |
| 尾段刷新 | 只从最后缓存交易日重取；同一 session 新增 revision | 第二次调用 start 等于最后 session；revision 数 +1；最新 close 使用修订值 |
| 跨周期共享 | 先取 5m，再取 15m，供应商调用不增加 | 5m 冷取 1 次；15m 请求仍为 1 次，输出 15m 聚合 bars |
| 精确截止 | 请求更早 as_of，供应商零新增请求 | 调用计数保持 1，返回截止时刻前的 bars |
| 后移截止 | 缓存块 cutoff 早于新 as_of，只重取目标 session | 新增 1 次请求，返回更晚的可用 bar |
| 源级锁 | 5m/15m 两个不同整包 key 同时请求，只允许一次供应商取数 | 两线程并发，provider 调用 1 次 |
| 供应商失败 | 保留已有块和 revision，记录失败原因 | 失败后 revision 数不变，`source_failures` 有记录 |
| 部分覆盖 | 保留旧尾部，记录缺失 session | 刷新返回旧完整尾部，失败表记录目标 session |

### 5.3 AAPL SIP 5m/60m 真实父子腿证据

使用同一 feed 的真实快照：

- 子周期 5m：`b7336d55d525ccf61cd8092243ba227fc2385dae78d0ec877704af06f418b976`，7016 根。
- 父周期 60m：`88d57fbbf7e213306709a2aa5d874cb399ca69d8551fc0050c71da914fa1e2fc`，629 根。
- 高级引擎 `hierarchical_v1`，`max_subwave_depth=1`，共享预算 50000。

实测：43 条 `WaveRelation`，其中 10 条 `VERIFIED`、33 条 `PROVISIONAL`，全部为 `EXACT_LEG`。示例父腿标签包括 `1`、`2`、`W`、`X`、`A`、`B`，均匹配 `ZIGZAG_ABC` 子结构。该样本证明真实重叠区间上的父腿→子结构映射可工作；未验证部分保留为 `PROVISIONAL`，未提升为已认证。

### 5.2 真实快照冷/热请求计数

把真实快照包装成带调用计数的 provider，使用隔离缓存运行 `fetch_bundle`：

| 数据 | 周期 | 冷取数 bars | 冷调用 | 热命中后调用 | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| AAPL IEX | 5m | 7016 | 1 | 1（未增加） | 同一 snapshot |
| AAPL IEX | 5m→60m 共享源块 | 629 | 1（5m 冷取） | 1（未增加） | 60m 由 5m 源块聚合 |
| NVDA SIP | 60m | 630 | 1 | 1（未增加） | 同一 snapshot |

真实快照来源：AAPL IEX `8b3b30874041c6a1d0369f60d34909a63a60efe500faf967827b63c4439f40ed`，NVDA SIP `a994efbaf82a5e98cf41e224d7e64f0cba7824f268d52f0dae95f77005b19725`。计数 provider 只做真实快照的时间切片，不访问网络。

### 5.4 IN-02 范围说明：日/周不改为源块

源块缓存按 **盘中源周期** 设计，覆盖 1m/5m/15m/30m/60m 的交易日块、尾段刷新和跨周期共享。日线和周线继续使用既有整包快照缓存：它们的历史跨度长、供应商分页和整包内容寻址已能提供精确截止复用，拆成日块不会改善本轮的盘中请求次数，反而增加日/周两套一致性路径。

因此本轮明确将日/周源块排除在 IN-02 范围外；日/周仍保留 `SnapshotStore` 的整包缓存、`save_at_cutoff` 和旧快照兼容读取。若后续需要日/周级增量，再单独设计日块和周末聚合缓存。

## 6. 验证命令与结果

```text
.venv/bin/python -m pytest tests -q              568 passed
.venv/bin/ruff check src tests scripts           PASS
.venv/bin/ruff format --check src tests scripts  PASS
.venv/bin/python -m pip check                    No broken requirements found
git diff --check                                 PASS
```

Wheel：

```text
elliottwaver-0.6.0-py3-none-any.whl
sha256 077e6f708a14ad03d4b2542ed937bc041ff9ca758b5a41b6210e2294d8f5eb6d
```

## 7. 未完成项与原因

1. **IN-02 剩余边界**：盘中源块、尾段刷新、修订、跨周期共享、源级锁和失败记录已完成；日线和周线仍走整包快照缓存，供应商页面级部分响应仍只能按已返回 bundle 的覆盖范围记录。
2. **IN-03 剩余边界**：盘中父子腿映射已通过合成和 AAPL SIP 5m/60m 真实重叠样本；未验证父腿保留为 `PROVISIONAL`。日周 `timeframe_link` 保持原路径。
3. **IN-06 真实覆盖**：CN 指数已用 Tencent 免费源完成 000001/000300/399001 全五周期；Tushare 授权源仍未验证。BRK.B IEX 的 1m/15m/30m/60m 因稀疏缺口为 `INSUFFICIENT_DATA`，已作为真实限制记录；真实冷/热请求计数和合成简报 p50/p95 已完成。
4. **IN-07 真实权限**：Yahoo 免费源已完成真实 SPX/NDX 全五周期；Massive 授权指数适配器和假响应测试已完成，但 `MASSIVE_API_KEY`/指数计划回执未取得，Massive 真实逐周期证据待补。

这些限制不影响已经通过的离线 5B 观察窗、同日历史、结算、评估和旧快照兼容路径；后续取得权限后按同一记录追加真实组合。
