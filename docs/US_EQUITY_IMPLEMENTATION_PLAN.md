# ElliottWaver 美股日用版实施与验收计划

实施基线为 `v0.1.0`。首版交付是用 Alpaca 的已完成美股日线和完整周线，对上市股票及 ETF 生成可复核的艾略特波浪分析。日线默认附周线背景，报告展示起点、主备浪型、确认、硬失效、阶段退出和目标区。验收以数据身份、时点与规则是否正确为准。事件概率按现有独立研究流程处理。

> **实施状态（2026-11-20）**：实现提交 `2026-11-20 的“Add US equity daily data and report acceptance”`。US-01 至 US-04 已完成代码、冻结响应和离线入口验收；US-05 已完成 AAPL、BRK.B、SPY 的真实 SIP 小样本和 AAPL 的 IEX 样本联调。真实快照、命令、手算核对和范围限制见[美股日用版验收](reviews/2026-11-20-us-equity-acceptance.md)。

## 范围与完成顺序

| 工单 | 工作 | 完成条件 |
| --- | --- | --- |
| US-01 | Alpaca 资产身份、分页与失败处理 | 同一标的、feed 和复权口径的完整数据才进入快照。错标的、权限失败、无效数据可复现且有明确错误。 |
| US-02 | 纽约交易日、日线可用时间、完整周及复权 | DST、假期缩短周、提前收盘、缺失会话、拆股都有独立预期，线上与回放同口径。 |
| US-03 | 日常入口、报告、缓存与自选 | 单标的和批量都能看清实际数据源及截止；同一快照正常入口、离线入口和报告一致。 |
| US-04 | 波浪规则、回放与结算复验 | 共享规则在美股价格窗口得到手算与冻结行情复验，无法验证的失效或结算保留待定。 |
| US-05 | 真实账户联调、手册及交付 | 可复现的真实 SIP 样本覆盖股票、带类别后缀股票和 ETF，完成离线复跑、全量回归与发布记录。 |

首批烟测使用 `AAPL`、`BRK.B`（同测 `BRK-B`）和 `SPY`。`SPY`、`QQQ` 是可直接分析的 ETF。《市场概况》若使用它们，报告保留 ETF 的代码和名称。盘中目前沿用 `analyze` 的既有 RTH 入口，`brief` 日用版验收聚焦 `1d/1w`。原生指数如 `SPX`、`NDX` 需要另有明确授权来源与独立标的身份，再开工单。纳入后续的还有提前生成 RTH 日线，以及更广标的和盘中覆盖。正式交易、账户查询及下单不在分析程序的业务范围内。

## 当前代码与已核对的缺口

下文包内 Python 路径相对于 `src/elliottwaver/`。测试、脚本和文档的路径相对于仓库根目录。

- `data/alpaca_provider.py` 已调用 Alpaca Market Data v2，显式传 `feed`、`adjustment`、`asof`、`start/end`，读取全部分页并拒绝重复 token。现有 `tests/test_alpaca_contract.py` 覆盖 raw/all 和 RTH 聚合。真实账户联调仍缺证据。
- **已复现**资产身份错配。用假响应令 `GET /v2/assets/AAPL` 返回 `symbol=MSFT` 及 MSFT 行情，当前 `AlpacaProvider.fetch` 生成 `canonical_symbol=AAPL`、`provider_symbol=MSFT`、`identity_verified=True` 的 bundle。US-01 首先修复此路径。
- `data/workflow.py`、`service.py` 和 `cli/main.py` 已共用取数、分析和摘要入口。`watchlist` 用 `--config` 选择美股 feed。现有工程能力不作为本轮真实美股验收的证明。
- 可参考同类美股扫描器对目标会话与 SIP 访问截止的检查，以及提前收盘边界。ElliottWaver 继续复用 `exchange_calendars` 和现有 provider、快照及交易日工具。

## US-01　核对资产身份和 Alpaca 响应

入口复用 `config.py` 的 `alpaca_credentials()` 和 `alpaca_asset_endpoint()`。数据从 `https://data.alpaca.markets/v2/stocks/bars` 获取，资产身份由已配置的官方 paper 或 live 资产端点核对。美股联网需要 `ALPACA_API_KEY`、`ALPACA_API_SECRET`。沿用兼容变量 `ALPACA_SECRET_KEY` 的冲突检查和现有 `.env.example`。仅保留状态、错误类别及上下文做诊断，测试用模拟凭证。 [Alpaca 历史 K 线契约](https://docs.alpaca.markets/us/reference/stockbars)

1. 在 `data/alpaca_provider.py` 抽查资产响应，确认资产 ID 为非空字符串、类别为 `us_equity`，返回代码与 `route_symbol()` 的规范 provider 代码一致。可复用 `data/router.py` 已明确支持的类别股别名。合法的历史更名由 bars 请求的 `asof` 解决，且以已核对的资产身份为根。遇到身份不明、缺代码、返回不同资产或不支持的资产类型，在发出行情请求之前终止。验收调用 `AAPL` 错返 `MSFT` 时 bars 请求次数为零。`BRK-B` 和 `BRK.B` 得到同一个资产 ID。
2. 保持两次 raw/all 请求的 feed、标的、日期窗、周期、`asof` 一致，各自消费所有分页。校验时间戳必须带时区，解码异常按现有数据源错误上报。相同内容的重复记录可去重。同一时间戳的冲突值、乱序后无法一一配对、超出交易日历的有效记录以及 raw/all 不对齐均须显式拒绝或诊断。`MarketDataBundle` 保持现有严格校验，快照入口接到不合法数据时不留下成功记录。测试覆盖空页带 token、第二页正常、重复 token、尾页失败及缺末日、raw 缺一日和无时区时间。
3. 沿用 `data/http.py` 的有界请求。401/403、配置缺失、格式错误和足额取数失败应给出安全且可区分的诊断。429 和短暂网络故障使用现有重试预算，`Retry-After` 超出总期限则结束。测试记录请求次数并核对 403 之后不存在 IEX 请求。日线用户可以显式重跑 `--feed iex`，报告把 IEX 标为单交易所口径。历史 SIP 的访问限制用显式 `end` 与现有缓冲满足，最终以真实账户响应为准。 [Alpaca 权限和 SIP/IEX 说明](https://docs.alpaca.markets/us/docs/market-data-faq)

## US-02　锁定交易日、完整周和价格坐标

位置为 `data/calendar.py`、`data/periods.py`、`data/workflow.py` 和 `service.py`。把三种时间分别写入诊断和验收预期。

| 字段或概念 | 口径 |
| --- | --- |
| `session_date`、`market_asof` | `America/New_York` 的交易日期。禁止用机器时区给美股 bar 分配会话。 |
| RTH 开盘和收盘 | 用交易日历的 `open_at/close_at`，支持 DST 和提前休市。 |
| 原生日线的 `available_at` | 维持 `provider_native_daily`。当地次日 00:00 加现有默认 20 分钟缓冲。`--as-of YYYY-MM-DD` 对美股使用同一截止。 |
| 请求 `end` | 取目标 `session_date` 的当地日线结束与本次发请求时允许访问的截止二者中的较早者，保持显式。过滤响应中非目标窗内的日期并验证最后有效日。 |

1. 使用交易日历先确定应有会话，再验证 raw 与 all 都覆盖目标日。交易量为零、供应商没有生成 bar、暂停交易都按能够证明的数据处理；真实缺口需在已有质量状态中暴露。测试缺应有尾日时返回陈旧或数据不足，周末和假日本身不产生缺口。通过固定 `now`、`as_of` 和请求参数捕获验证线上、缓存命中、离线和回放使用同一会话。纽约冬令时原生日线的次日可用时刻是 `05:20Z`，夏令时是 `04:20Z`，测试点取 2024-03-08 与 2024-03-11 的各自下一日。
2. 周线使用同一份已验证日线的交易日期和分析价聚合。开盘取首根，最高最低取区间极值，收盘取最后一根，成交量求和。只取周内预定会话齐备且已可用的完整周，先按供应商分析价取齐日线再聚合。覆盖 2024-03-29 休市的缩短周、2024-11-29 提前收盘、周中请求和缺内部一日。周中已有日线可报告，周线使用上一个完整周。既有 RTH 回归中，13:00 ET 提前收盘应保留 12:30–13:00 的半小时尾段。 [Alpaca 交易日历](https://docs.alpaca.markets/us/reference/legacycalendar)
3. 引擎只读取 `provider_all` 分析价，raw 保留作核对和坐标校验。保持 `price_coordinate_id`、`basis_date`、`factor_hash`、`source_snapshot_id` 与现有 `MarketDataBundle` 契约一致。手算一个 2:1 拆股例及一个现金分红例，检查日线、当周 OHLC、目标区和失效条件都在同一个价格坐标。旧快照继续按其各自身份读取，新的版本或口径与旧产物通过现有历史状态说明。供应商调整历史回放须标记为冻结数据回放。 [Alpaca 复权参数](https://docs.alpaca.markets/us/reference/stockbars)

## US-03　打通日用入口并显示行情来源

以 `cli/main.py` 的 `run_analyze`、`_run_brief_core`、`run_fetch`、`run_watchlist` 为入口，继续共用 `fetch_bundle` 与 `AnalysisService`。优化限于真实输出中缺失的元信息与诊断。命令、已发布报告的业务摘要以及旧 JSON 契约保持兼容。Markdown、HTML、终端用同一份已验证报告元信息显示 feed、复权、行情日期、分析截止及运行模式。细分的 `session_profile` 和时间字段可从源快照、`data inspect` 或现有 manifest 查到。必要的模式说明通过现有 issues/warnings 表达。

| 路径 | 独立验收 |
| --- | --- |
| `analyze AAPL --feed sip`、`brief AAPL --feed sip` | 引擎一致。用同一冻结快照、截止、窗口和设置核对日线上下文、场景 ID、端点、条件、目标与质量分。 |
| `brief AAPL --timeframe 1w --feed sip` | 周线主分析和从日线派生的独立周线结果一致；比较完整周的实际行情日期。 |
| `data fetch`、`data inspect`、`brief --offline --snapshot` | 快照摘要、资产、feed、日历、复权和价格坐标一致。离线显式指定相反 feed 或另一标的时拒绝。 |
| `data/workflow.py`、`data/snapshots.py` 与 `history.py` | 同一标的的 SIP、IEX 使用各自缓存、报告和历史身份。两组不同 OHLC 回归验证不会串读；历史对比若跨 feed，按既有 `NOT_COMPARABLE` 路径处理。 |
| `watchlist --config` | 使用现有 `data.us_feed` 选择美股 feed。例表覆盖两个合法美股别名、一个沪深标的和一个出错标的，核对去重、报告 feed、单项失败和退出码。批量并发先使用现有小并发默认值。 |

将命令、HTML 文件位置、`UNCLEAR`、陈旧/缺数据、权限失败的处理办法写到 `docs/USER_GUIDE.md` 的美股小节。用户先看行情日期、时区、复权和 feed，再读候选规则。`doctor --market US` 仍用于本地依赖与配置检查；实际账户权限由真实 fetch 证明。取数失败时可以指定已有快照复盘，报告保留原行情日期。

## US-04　验收波浪规则和结算

1. 使用与 A 股相同的模板、硬规则、子浪和投影实现。开工从 `tests/test_engine.py` 选出推动浪、锯齿或三角形至少三类代表用例，使用美元价坐标构建可手算的上涨、下跌和进行中端点例子。另覆盖影线跨界和跳空越过边界。对端点、硬规则状态及确认、失效、阶段退出逐项核对。只有明确规则与反例证明共享引擎有问题时，才修改引擎并加 A 股回归。
2. 同一冻结快照选一个判断日 T。离线分析 T、物理删除未来行后分析 T，两个结果的可用端点、候选集和条件价一致。T 后走势只影响后续分析及结算。`replay` 的输出记录源快照、模式和可用截止，行情修订及复权变化沿用历史比较状态。
3. 使用 `evaluation/outcomes.py`、`settlement.py` 和当前 `provider_price_scale` 结算。新快照必须匹配同一资产、feed 和周期；可验证的拆股桥接在发行坐标核算，无法验证返回 `PRICE_BASIS_UNVERIFIED`。选已知目标/失效路径给出手算镜像。N 个交易日由交易日历推进，缺中间日期不能顺延期限。用一根 K 线同时触达两条边界做歧义回归。

## US-05　真实行情联调和交付

先跑全量离线测试，再在能够访问 Alpaca、已配置授权凭证的环境执行。创建 `reports/us-acceptance/`，将以下配置保存为该目录的 `config.yaml`。

```yaml
data:
  us_feed: sip
cache:
  directory: .cache/us-acceptance
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,cn,us]'
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/elliottwaver doctor --market US
US_SMOKE_CONFIG=reports/us-acceptance/config.yaml
.venv/bin/elliottwaver data fetch AAPL --timeframe 1d --feed sip --config "$US_SMOKE_CONFIG"
.venv/bin/elliottwaver brief BRK.B --feed sip --config "$US_SMOKE_CONFIG" --output-dir reports/us-acceptance
.venv/bin/elliottwaver brief SPY --timeframe 1w --feed sip --config "$US_SMOKE_CONFIG" --output-dir reports/us-acceptance
```

将 fetch 返回的 AAPL 快照 ID 交给 `data inspect`、`brief --offline --snapshot` 和同窗的 `analyze --offline`。为 AAPL、BRK.B 和 SPY 保存有效快照 ID 与 HTML 摘要。对一只标的单独请求 IEX 并保留结果或权限证据。IEX 的可用范围按实际结果标注。用手算核对一个原生日 OHLC、一个完整周 OHLC、一个带规则证据的候选、一个结果不明确的案例或冻结反例。读取已发布的事件报告时使用 `read_brief_report` 核验文件和身份。

交付 `docs/reviews/YYYY-MM-DD-us-equity-acceptance.md` 并加入索引，记录提交、测试、资产/周期、实际 feed、行情日期、数据可用时刻、快照 ID、通过项和阻塞。可重放的输出在 `reports/us-acceptance/`；生成文件和账户信息按仓库惯例排除出 Git。美国账户联调缺凭证、网络或 SIP 权限时，完整提交离线证据，真实 SIP 发布状态标记为待验证。第一批正式美股支持以 US-01 至 US-05 的实际证据验收。更新 `README.md`、`docs/USER_GUIDE.md` 和 `docs/IMPLEMENTATION_STATUS.md` 后，由验收者核对安装包和发布范围。

## 交给 DeepSeek 的执行说明

从 `v0.1.0` 的现有实现出发，按 US-01 到 US-05 顺序推进。先为已复现身份缺陷取一个失败用例，再修共用数据路径。每项列出改动文件、精确复现命令和实测结果。参考仓库仅用于阅读行情处理方法。保留现有 A 股兼容性，避免无关重构或更改波浪评分。离线证据、真实账户证据及未验证项各自记录，完成后提交验收材料给维护者。
