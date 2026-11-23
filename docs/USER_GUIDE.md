# ElliottWaver 中文使用手册

适用于 ElliottWaver 0.6.0。日用分析覆盖沪深个股与指数、美股个股与 ETF 的日线和周线。逐腿结构树和日周关联通过增强配置启用。 [升级步骤](#从已有安装升级)

ElliottWaver 从股票和指数的 K 线中识别波浪候选，给出规则检查、推进价、失效价和目标区。日常用 `brief` 直接在终端看数浪和短期推演，单周期深入分析用 `analyze`。需要图表时，可另行打开 HTML 摘要。

目录：

- [1. 先运行起来](#1-先运行起来)
- [2. 安装与环境检查](#2-安装与环境检查)
- [3. 股票代码与 K 线周期](#3-股票代码与-k-线周期)
- [4. 分析 A 股与美股](#4-分析-a-股与美股)
  - [高级多浪级引擎](#431-高级多浪级引擎实验)
  - [收盘摘要与历史查询](#45-一页收盘摘要-brief)
  - [自选批量、到期结算和支持矩阵](#47-自选批量)
  - [离线评估与概率校准](#48-离线评估与概率研究)
  - [盲标与一致性评分](#49-盲标导出与一致性评分)
- [5. 找到并查看报告](#5-找到并查看报告)
- [6. 怎么理解分析结果](#6-怎么理解分析结果)
- [7. 日期、时间和锚点](#7-日期时间和锚点)
- [8. 保存快照与离线分析](#8-保存快照与离线分析)
- [9. 历史回放](#9-历史回放)
- [10. 配置与批量处理](#10-配置与批量处理)
- [11. 常用参数速查](#11-常用参数速查)
- [12. 常见问题](#12-常见问题)

## 1. 先运行起来

以下示例从源码目录运行。先安装对应市场的依赖，再运行 `brief` 读取可用的收盘数据。[安装步骤](#2-安装与环境检查)

```bash
.venv/bin/elliottwaver brief 600519.SH --format json
.venv/bin/elliottwaver brief 000300.SH --timeframe 1w --format json
```

第一条命令查看贵州茅台的日线主分析和最近完整的周线背景。第二条命令用周线分析沪深 300。两条命令都从新浪取数，需要联网，无需密钥。终端显示中文摘要及保存路径，文件只保存 JSON。需要看 K 线图时，可去掉 `--format json`，加 `--open` 打开默认生成的网页摘要。

配置 Alpaca 后，可用 `.venv/bin/elliottwaver brief AAPL --feed sip --format json` 查看美股日线摘要。[美股配置与示例](#42-美股)

每天先核对行情日期和数据状态，再看主备浪型、目标区和改变判断的价位。摘要显示「判断尚不明确」时，先保留多种解释。来源不可用时可指定已有快照复查原行情。[报告阅读顺序](#45-一页收盘摘要-brief) · [快照复查](#8-保存快照与离线分析)

没有网络时，先运行合成行情演示。将终端打印的完整「快照」ID 填入变量，再生成离线摘要。

```bash
.venv/bin/elliottwaver demo --symbol 600519 --timeframe 1d
DEMO_SNAPSHOT_ID='替换为演示命令打印的完整快照ID'
.venv/bin/elliottwaver brief 600519 --offline --snapshot "$DEMO_SNAPSHOT_ID" --format json
```

演示使用合成行情，`brief` 会显示「使用已保存行情回放」及决策截止时刻。真实快照的离线用法见[保存快照与离线分析](#8-保存快照与离线分析)。

## 2. 安装与环境检查

新机器需要 Python 3.11 或更高版本。发布验证使用 Linux / Python 3.12。

### 从源码安装日用版

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[cn,us]'
.venv/bin/elliottwaver --version
.venv/bin/elliottwaver doctor --market CN
.venv/bin/elliottwaver doctor --market US
```

### 从已有安装升级

0.6.0 起项目更名为 **ElliottWaver**：Python 包为 `elliottwaver`，CLI 命令为 `elliottwaver`，环境变量前缀为 `ELLIOTTWAVER_`，默认快照目录为 `.cache/elliottwaver/`。旧版 `eliot` 命令和 `ELIOT_` 环境变量不再保留兼容别名。

已有 `.venv` 时，从更新后的源码目录重新安装，并按需迁移旧缓存：

```bash
.venv/bin/python -m pip install -e '.[cn,us]'
# 如需沿用旧快照，可移动缓存目录，或在配置中显式指定 cache.directory
mv .cache/eliotbot .cache/elliottwaver
```

`brief`、`analyze`、`history` 和自选批量的参数与结果格式保持兼容。原 `config.yaml` 中如仍写着旧缓存目录，可继续使用，也可改为 `.cache/elliottwaver`。 [版本说明](../CHANGELOG.md#060--2026-11-22)

### 从发布 Wheel 安装

将发布文件 `elliottwaver-0.6.0-py3-none-any.whl` 放到当前目录。安装依赖需能访问所用 Python 包源。首次运行请在有写权限的工作目录执行。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install './elliottwaver-0.6.0-py3-none-any.whl[cn,us]'
.venv/bin/elliottwaver --version
.venv/bin/elliottwaver demo --symbol 600519.SH
```

单市场安装可选择 `[cn]` 或 `[us]`。用源码包可运行 `scripts/` 中的研究、回放和验收脚本。开发和测试用 `-e '.[dev,cn,us]'`。

### 环境检查与锁定依赖

如需使用本项目记录的依赖版本：

```bash
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

锁定文件记录本次 Python 3.12 / Linux 环境的完整安装版本；其他系统仍需满足相应包的安装条件。

`doctor` 会列出依赖版本、支持的周期、浪型，以及是否配置 Alpaca 凭证：

| 字段或现象 | 含义 |
| --- | --- |
| `supported_patterns` | 引擎支持的浪型，包括推动浪、锯齿、三角形、平台、楔形和 WXY |
| `alpaca_credentials_configured: false` | 未配置美股凭证；不影响 A 股和离线演示 |
| `kaleido: null` | 未安装 Plotly 的 PNG 导出依赖；不影响 HTML、Markdown 或 SVG 渲染器的 PNG |
| `network_smoke: NOT_RUN` | 该命令没有测试网络，也没有验证真实行情权限 |
| `index_timeframes: ["1d","1w"]`；`bj_live: INDEX_ONLY` | 指数只支持日线/周线；北交所仅指数接入，个股未启用 |

不想每次输入 `.venv/bin/` 时，可以在当前终端激活环境：

```bash
source .venv/bin/activate
elliottwaver analyze 600519 --timeframe 1d
```

下面仍保留完整路径，便于直接复制。

## 3. 股票代码与 K 线周期

沪深股票可以输入 `600519`、`600519.SH`、`sh600519`、`000001`、`300750` 等形式。美股使用 `AAPL`、`MSFT` 等代码，已明确支持的股份类别别名包括 `BRK-B` → `BRK.B`。最终证券身份还会由行情源核验。

沪深京指数使用 `000688.SH`、`sh000688`、`399001.SZ`、`899050.BJ` 等形式。`000001` / `000001.SZ` 是平安银行，`000001.SH` 才是上证指数；上证指数必须带 `.SH` 后缀或 `sh` 前缀。当前识别规则为：

| 范围 | 识别为指数 | 示例 |
| --- | --- | --- |
| 上证指数 | `SH000*` | `000001.SH`、`000300.SH`、`000688.SH` |
| 深证指数 | `SZ399*` | `399001.SZ`、`399006.SZ` |
| 北证指数 | `BJ899*` | `899050.BJ` |

`SZ000*` 仍是深市个股，`SH688*` / `SH689*` 仍是科创板个股。`SH880*` 同花顺行业指数当前不接入。指数默认只支持日线和周线；配置 `data.cn_index_intraday_feed: tushare-cn-index` 并提供 `TUSHARE_TOKEN` 后，才开放指数分钟线。未配置时请求指数分钟线返回 `INDEX_INTRADAY_UNSUPPORTED`。

当前线上范围为沪深 A 股、沪深京指数与美股个股；北交所个股尚未接通，港股尚未实现。

| 周期 | 参数 | 示例 |
| --- | --- | --- |
| 日线 | `1d`，默认值 | `elliottwaver analyze 600519 --timeframe 1d` |
| 周线 | `1w` | `elliottwaver analyze 600519 --timeframe 1w` |
| 小时线 | `60m` 或 `1h` | `elliottwaver analyze 600519 --timeframe 1h` |
| 30 分钟 | `30m` | `elliottwaver analyze 600519 --timeframe 30m` |
| 15 分钟 | `15m` | `elliottwaver analyze 600519 --timeframe 15m` |
| 5 分钟 | `5m` | `elliottwaver analyze 600519 --timeframe 5m` |
| 1 分钟 | `1m` | `elliottwaver analyze 600519 --timeframe 1m` |

表格中的短命令需要先激活虚拟环境。月线和 4 小时线不能作为当前主分析周期。指数默认只支持 `1d` 和 `1w`；配置 Tushare 指数分钟源后可请求 `1m/5m/15m/30m/60m`。沪深个股默认使用新浪分钟源，也可配置 `data.cn_equity_intraday_feed: tushare-cn-equity`。

每次调用分析一个主周期。A 股小时线不跨午休；美股分钟/小时线从常规交易时段（RTH）开盘对齐，收市前可能保留不足整小时的尾段。周线先对日线进行复权，再聚合完整交易周，尚未结束的周不会纳入。

分钟历史长度由供应商实际返回范围决定。增加 `history_years` 或指定更早的 `--start`，不能保证取到供应商未提供的分钟数据。

## 4. 分析 A 股与美股

### 4.1 沪深 A 股

无需 API key。日线、周线、小时线和分钟线使用同一命令：

```bash
.venv/bin/elliottwaver analyze 600519
.venv/bin/elliottwaver analyze 000001 --timeframe 1w
.venv/bin/elliottwaver analyze 300750 --timeframe 1h
.venv/bin/elliottwaver analyze 600519 --timeframe 15m --top-k 5
```

默认显示最多 3 个达到门槛的候选，`--top-k` 可以设为 1–20；增加这个值不会强行生成候选。

希望重新从行情源获取数据时使用：

```bash
.venv/bin/elliottwaver analyze 600519 --timeframe 1d --refresh
```

`--refresh` 跳过缓存复用，不会删除旧快照。

### 4.2 美股

首次使用时，在项目根目录创建 `.env`。已有该文件时直接编辑，保留原有配置：

```bash
cp -n .env.example .env
```

填写以下内容，使用与密钥对应的 paper 或 live 账户端点：

```dotenv
ALPACA_API_KEY=你的密钥
ALPACA_API_SECRET=你的密钥密码
ALPACA_API_ENDPOINT=https://paper-api.alpaca.markets
```

live 端点为 `https://api.alpaca.markets`。配置文件从当前工作目录读取，因此应在项目根目录执行示例。

```bash
.venv/bin/elliottwaver doctor --market US
.venv/bin/elliottwaver brief AAPL --feed sip
.venv/bin/elliottwaver analyze AAPL --feed sip
.venv/bin/elliottwaver analyze MSFT --timeframe 1h --feed sip
.venv/bin/elliottwaver analyze AAPL --timeframe 15m --feed iex
```

默认使用 `sip`；`iex` 必须显式指定，或通过配置修改。需要账户具有所选 feed 的访问权限，权限失败会报错，不会自动更换 feed。不同 feed 的结果会标记来源并分别缓存。

取数前会先核对 Alpaca 资产端点返回的资产 ID、`us_equity` 类别和规范代码；返回不同标的时会在发出 K 线请求前终止。`BRK-B` 与 `BRK.B` 会解析到同一个资产。`data fetch` 会输出快照 ID、feed、复权口径、行情截止、分析截止和最后可用时刻；可用 `data inspect` 复核来源，再用 `brief --offline --snapshot` 或 `analyze --offline --snapshot` 离线复跑：

```bash
.venv/bin/elliottwaver data fetch AAPL --timeframe 1d --feed sip
.venv/bin/elliottwaver data inspect <snapshot-id>
.venv/bin/elliottwaver brief AAPL --offline --snapshot <snapshot-id> --feed sip
.venv/bin/elliottwaver analyze AAPL --offline --snapshot <snapshot-id> --feed sip
```

离线入口会拒绝与快照不同的 feed 或标的。`brief` 的 JSON、Markdown、HTML 和终端摘要都会显示 `feed`、`adjustment`、`market_asof`、`as_of` 和 `mode`；批量自选也会在每条记录中保留来源和单项失败。

美股原生日线按供应商日线口径处理：程序保守等到纽约当地次日零点再加访问缓冲后，才纳入该日线。分钟/小时线采用 RTH 口径，两者的数据截止时刻可能不同。周线只使用同一份已验证日线中会话齐备的完整周，周中请求使用上一个完整周。

当前美股适配器已通过冻结响应、身份错配、分页、DST、假期、提前收盘、完整周、复权和离线入口测试；AAPL、BRK.B、SPY 的真实 SIP 小样本和 AAPL 的 IEX 样本也已通过，验收状态见[美股日用版验收](reviews/2026-11-20-us-equity-acceptance.md)。

### 4.3 沪深京指数

无需 API key。指数使用新浪指数日线，来源标记为 `sina-index`，调整口径为 `none`，不计算复权因子。目前支持日线和周线：

```bash
.venv/bin/elliottwaver analyze 000688.SH --timeframe 1d
.venv/bin/elliottwaver analyze 000300.SH --timeframe 1w
.venv/bin/elliottwaver analyze 399001.SZ --timeframe 1d
.venv/bin/elliottwaver analyze 899050.BJ --timeframe 1d
```

指数报告会显示“品种：指数；指数无复权，价格为原始点位”。`000001` 默认仍是平安银行；要分析上证指数请写 `000001.SH`。

指数默认只取最近 `history_years`（默认 5 年）；分析更长历史时设置 `analysis.history_years` 或 `--start`。例如：

```bash
.venv/bin/elliottwaver analyze 000688.SH --timeframe 1w --start 2020-01-01
```

长期数浪还需要按需提高 `analysis.max_analysis_bars`（默认 600，最大 5000），否则锚点搜索只覆盖最近约 600 根 K 线。指数分钟线尚未启用；请求 `5m` 等周期会返回 `INDEX_INTRADAY_UNSUPPORTED`。

### 4.3.1 高级多浪级引擎（实验）

日用默认采用 `classic_v4`。需要逐腿结构树或日周关联时，可直接传入 `--config config.advanced.example.yaml`。样例在源码包根目录，使用 wheel 时可把样例复制到当前工作目录。增强模式支持日线和周线。自定义时复制样例配置并修改所需参数。

```yaml
analysis:
  engine_profile: hierarchical_v1
  scales: [1.0, 1.4, 2.4, 3.8]
search:
  max_subwave_depth: 1
```

`scales` 必须包含比父级更细的尺度，否则父腿只能显示 `PENDING/INSUFFICIENT_FINER_PIVOTS`。`max_subwave_depth` 为 0 时保留端点，1 验证第一层子浪，2 继续验证第二层。一次分析可用 `--subwave-depth` 覆盖样例的默认深度 1。深度 2 需要更多可确认的内部拐点。若摘要显示 `SEARCH_LIMITED`，可去掉 `--subwave-depth 2`，先按样例的深度 1 复查。

联网分析可直接运行以下命令。

```bash
.venv/bin/elliottwaver brief 600519.SH --config config.advanced.example.yaml --open
.venv/bin/elliottwaver brief 600519.SH --config config.advanced.example.yaml --subwave-depth 2 --open
```

离线时将 `SNAPSHOT_ID` 设为该标的的日线快照 ID。日线摘要可以从这份快照生成完整周线背景，单周期 `analyze` 可直接选择周线。 [保存快照与离线分析](#8-保存快照与离线分析)

```bash
.venv/bin/elliottwaver brief 000016.SH --offline --snapshot "$SNAPSHOT_ID" --config config.advanced.example.yaml --json
.venv/bin/elliottwaver analyze 000016.SH --timeframe 1w --offline --snapshot "$SNAPSHOT_ID" --config config.advanced.example.yaml --json
```

高级结果：

- `schema_version` 为 `1.1`（分析）或 `1.2`（摘要），引擎标识含 `hierarchical-rules-v3`。
- 每个候选附有 `structure`，记录根节点、逐腿 `child_links`、节点规则及已验证深度。摘要 HTML 可展开子浪和日周关联，Markdown 与终端也输出结构证据。
- 日线摘要的 `wave_relations` 记录日线候选与周线父腿的 `EXACT_LEG`、`CONTAINED`、`INCOMPATIBLE` 或 `UNDETERMINED` 关系。
- 平台三变体、阻挡三角形、截短第五浪、收缩/引导/终结楔形和 WXY 双重三浪在端点与内部结构证据齐备时带 `variant`；引导/终结楔形内部按 3-3-3-3-3 认证，引导同时接受 5-3-5-3-5；未知父级位置不会强行给出引导/终结楔形结论。
- 自选高级用法从 `config.advanced.example.yaml` 开始。亚型的端到端正反例见 [修复复验](reviews/2026-11-20-wave-engine-repair.md)。

`VERIFIED` 表示整个结构达到请求的验证深度。历史日周关系也按该深度核验。仍缺证据时，可从报告查看已通过的层和待补的腿。确定违反硬规则的结构在排名前排除，搜索耗尽显示 `SEARCH_LIMITED`。历史摘要记录子浪证据的变化，条件触发值沿用发行时价格坐标，并保留冻结输入的 K 线 ID。旧 `classic_v4` 报告仍可按原 JSON 校验和核销。 [复验记录](reviews/2026-11-21-wave-engine-followup-repair.md)

### 4.4 长历史与大级别搜索

A 股老指数/老股票原始数据可到 1990/1991 年；引擎日历从 **1991-01-02** 起，更早的数据会被裁剪并给出 `CALENDAR_CLIPPED`。默认只下载 5 年、锚点只看最近 600 根日线，所以长历史需要显式开启：

```bash
# 20 年窗口、默认周线、Top-8、首尾兼顾的锚点、宽桥接搜索
.venv/bin/elliottwaver analyze 000001.SH --long-term --start 1991-01-01 --allow-gaps

# 只放宽搜索，不改变数据窗口
.venv/bin/elliottwaver analyze 000300.SH --large-degree --timeframe 1w

# 子浪规则认证（0/1/2）
.venv/bin/elliottwaver analyze 000001.SH --long-term --subwave-depth 1
```

`--long-term` 等价于：`history_years=20`、`max_analysis_bars=5000`、默认 `1w`、`top_k=8`、`anchor_mode=spread`、`max_bridge_intervals=21`、`beam_width=512`、`max_expansions=200000`、`max_subwave_depth=1`。`--large-degree` 只应用后三项搜索参数，不改变子浪深度。

30 年以上日线会超过 `max_analysis_bars` 上限 5000；请用周线，或接受 `ANALYSIS_WINDOW_TRUNCATED` 警告。个股长历史通常有停牌缺口；`--allow-gaps` 会继续分析并给出 `ALLOWED_GAPS` 警告，不补造价格。

### 4.5 一页收盘摘要 `brief`

`brief` 自动选择主周期和起点。下面展示如何调整主周期、日期和输出方式。

```bash
.venv/bin/elliottwaver brief 600519 --timeframe 1d
.venv/bin/elliottwaver brief 600519 --start 2015-01-01
.venv/bin/elliottwaver brief 600519 --timeframe 1d --no-weekly --no-window-check --format json --json
```

默认 `--timeframe auto` 选择日线。输入原生周线快照，或 `--start` 到截止时刻超过 5 × 365 天时，自动选周线。输入盘中快照时，自动采用该快照的分钟周期。显式指定 `--timeframe 1d`、`1w` 或 `1m/5m/15m/30m/60m` 可锁定周期，`1h` 等价于 `60m`。`--start` 设置搜索下界，`--anchor YYYY-MM-DD` 指定起浪日；盘中还可使用带时区的精确锚点。需要 `--long-term` 等搜索预设时，使用前文的 `analyze` 示例。

盘中示例：

```bash
.venv/bin/elliottwaver brief 600519.SH --timeframe 60m --format json
.venv/bin/elliottwaver brief AAPL --timeframe 5m --feed sip --format json
.venv/bin/elliottwaver brief AAPL --offline --snapshot "$SNAPSHOT_ID" --format json
```

打开摘要后按下面的顺序阅读。

1. **先看当前判断。** 首屏显示主备浪型、当前浪和后续观察期限。日线观察发行后的 5 个完整交易日，周线观察 2 个完整交易周，盘中观察发行后尚未开始的 5 根主周期 K 线。盘中首屏同时显示市场当地时间、决策截止和最后一根已完成 K 线的可用时刻。目标可以立即观察，也可能要等推进信号。报告列明看最高价、最低价还是收盘价，以及等于边界时是否触发。触发价保留比较精度，标有「约」的目标区仅在显示时简化，小数边界可查 JSON。
2. **再看何时重新数浪。** 「失效条件」满足后会否定当前候选或浪段。「需重新数浪」表示阶段退出，需要更新当前判断。证据不足、行情过期或日历不全时，摘要会说明短期观察为何不可用。日周方向、换起点复核和上一次的判断变化用中文显示。
3. **需要时查看核对依据。** 这里有决策截止时间、分析范围、起点来源、子浪证据和评分。质量分衡量形态吻合程度，证据覆盖率是可用评分证据的比例，候选权重表示本次候选之间的相对支持度。概率模型的证据不足时，报告会说明概率尚未校准。 [概率研究与结果解读](#48-离线评估与概率研究)
4. **核对原始字段。** 显式指定快照、`--as-of` 或 `--end` 的人读报告显示「使用已保存行情回放」，JSON 的模式为 `FROZEN_DATA_REPLAY`。事件 ID、状态码、未展示候选及完整搜索诊断可从 `brief.json` 和 `daily.json` 查到。只需要终端和 JSON 时，可以加 `--format json`。

短期事件包括 `ADVANCE_FIRST_5D_V1`、`OUTLOOK_TARGET_FIRST_5D_V1`、周线对应版本，以及盘中 `ADVANCE_FIRST_{1M,5M,15M,30M,60M}_5B_V1` 和 `OUTLOOK_TARGET_FIRST_*_5B_V1`。周线另可核销 `CLOSE_BAND_2W_V1`。没有 `outlook` 字段的旧报告保持原事件清单；旧盘中报告若带周线 `2W` 观察窗，结算返回「旧观察窗与主周期不匹配」。发行、离线回放和 `settle` 使用同一组冻结交易期、价格坐标和观测截止；周中发行只从下一个完整周开始计算，日线发行跳过发行时已经开盘的会话，盘中发行只取 `start_at >= issued_at` 的主周期时隙。

`--no-weekly` 和 `--no-window-check` 分别关闭摘要的周线背景和换窗复核。主周期候选的终端摘要有规则状态计数，网页可展开「规则复核」，Markdown 列出各规则和实测值。规则状态沿用当次分析的 `PASS / FAIL / PENDING`，分别表示通过、违反和证据不足。结构化规则字段位于同目录的 `daily.json`。导出文件和目录见[找到并查看报告](#5-找到并查看报告)。

### 4.6 历史查询与上次判断

每次发布完整的 `brief` 后，程序把摘要和路径登记到本地 SQLite 索引。`history` 读取已有报告，`--mode live` 筛选日常记录，`--mode frozen` 筛选回放记录。按日期筛选时，`--as-of` 表示行情日期上界。

```bash
.venv/bin/elliottwaver history 600519
.venv/bin/elliottwaver history 600519 --mode frozen --json
.venv/bin/elliottwaver history AAPL --timeframe 5m --mode frozen --json
```

查询返回最近 50 条匹配记录。`--timeframe` 可按主周期筛选；盘中记录按最后一根已完成 K 线的时刻排序，并显示市场当地时间。JSON 数组中每条记录的 `path` 指向原报告目录。生成摘要时若用了自定义缓存，查询时也要传同一个 `--config`。报告移走或校验失败时，历史结果显示 `UNREADABLE`。

`brief.json` 中的 `previous` 记录同模式的上一次较早判断。盘中报告按最后一根已完成 K 线选择前一份记录，因此同一天的两份判断也能比较；重复生成同一根 K 线时沿用此前合格记录。常见结果有 `FIRST_RECORD`（首次）、`SAME_COUNT`（维持）、`ALTERNATE_PROMOTED`（备选升为主选）、`STAGE_ADVANCED`（浪段推进）和 `COUNT_CHANGED`（结构改变）。配置或引擎版本变化显示 `CONFIG_CHANGED`。周期、feed、复权口径变化或没有可比较的行情时显示 `NOT_COMPARABLE`。`DATA_REVISED` 只计算截至上次决策的重叠 K 线和因子修订。JSON 另保留 `previous_last_bar_end_at`、`structure_change`、`revision_count` 和 `factor_revision_count`。

### 4.7 自选批量

```bash
mkdir -p reports
printf '600519.SH\n000300.SH\n' > reports/my-watchlist.txt
.venv/bin/elliottwaver watchlist reports/my-watchlist.txt --workers 2 --retries 1 --json
```

文件每行一个标的，空行和以 `#` 开头的注释会被忽略，合法代码按规范写法去重。`--workers` 可设为 1、2、4 或 8，`--retries` 可设为 0 至 5，`--retry-delay` 设置两次尝试之间的秒数。非法代码按原文记录，其他标的继续处理，命令最后返回首个失败标的的退出码。已生成但状态为 `INSUFFICIENT_DATA` 或 `STALE_DATA` 的摘要也计为失败，可用 JSON 条目的 `path` 和 `error` 排查。

批量摘要只保存 JSON 产物，默认目录为 `reports/watchlist/`。要看某只标的的 HTML，单独运行 `brief SYMBOL`。如需给整批选用其他周期，传 `--timeframe 1w`。批量行情来源通过 `--config` 配置，美股仍需要对应 feed 的凭证。`--model-registry DIR` 可指定已有的模型注册表，默认使用缓存目录下的 `models/`。

### 4.7.1 到期结算

先生成 `brief` 并保留其报告目录。等要核对的交易日或交易周结束后下载新快照。以下示例假设贵州茅台原摘要的主周期是日线。

```bash
.venv/bin/elliottwaver data fetch 600519 --timeframe 1d --refresh
OUTCOME_SNAPSHOT_ID='替换为新下载行情的完整snapshot_id'
.venv/bin/elliottwaver settle reports/CN/600519.SH/brief \
  --snapshot "$OUTCOME_SNAPSHOT_ID" --output reports/settlements-600519.json --json
```

目录参数会递归读取报告，也可以传一份报告的 `brief.json`。结算只读取缓存，省略 `--snapshot` 时使用每份报告自己的源快照。替代快照应与报告的标的、周期、feed 和复权口径相容，并覆盖所需到期日。周线报告也可使用由日线快照派生的周线行情。混合标的或口径先按报告分别结算。使用自定义缓存时，传同一份 `--config` 或传 `--cache-root` 指向存放快照的目录。

- 查看 `records[].status` 和 `records[].latest.outcome`。进入 `SETTLED` 后可读实际标签。尚未覆盖必要行情时显示 `RIGHT_CENSORED`，应有交易会话缺数据时显示 `INCOMPLETE_DATA`。行情补齐后以新快照重跑。
- 结算使用报告发行时的收盘价、ATR、候选边界和预定会话。短期触价事件使用截止时已可用的完整 K 线。周线触价要求冻结名单内的每场会话齐备，周线收盘只取排定末场的收盘。盘中事件按冻结的 5 个精确时隙逐根核销，结果记录触发 K 线的 `bar_id`、起止时刻和价格口径；同根触及目标与失效时保留 `AMBIGUOUS_SAME_BAR`，前面有未知缺口时先记 `INCOMPLETE_DATA`。结算快照与发行的会话安排冲突时，短期事件显示 `SETTLEMENT_CALENDAR_MISMATCH` 并保留待核。美股换用修订的复权快照时，无法确认价格换算比例会显示 `PRICE_BASIS_UNVERIFIED`。
- 每个 `brief_digest + event_id + scope` 只算一次。同一判断导出为多个格式时会去重。行情修订后保留 `initial`，更新 `latest` 和 `latest_revision`。路径无效、没有报告或有报告/快照处理失败时，退出码为非零。

### 4.7.2 支持矩阵

```bash
.venv/bin/elliottwaver support-matrix
# 完成 4.8 训练后，可读所产出的模型汇总
.venv/bin/elliottwaver support-matrix \
  --model-summary reports/models/summary.json \
  --output reports/support-matrix.json --json
```

输出各资产类型、周期、事件的 `model_status`、`applicable_model` 和未通过原因。直接运行时显示 `UNCALIBRATED`。传入训练的 `summary.json` 时读该文件中的结果。检查某次分析实际加载的模型和概率时，查看该分析的 `brief.json`。

### 4.8 离线评估与概率研究

评估、训练与支持矩阵是可独立运行的研究流程。先用 `data inspect` 确认协议中的每个快照在指定的 `cache_root` 中。仓库保存了[四标的冻结协议及取数说明](protocols/README.md)。新环境按该说明准备自己的协议副本。

```bash
PROTOCOL=docs/protocols/roadmap-repair-2026-heldout-smoke.json
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol "$PROTOCOL" --output-dir reports/eval
.venv/bin/python scripts/train_forecasts.py \
  --protocol "$PROTOCOL" --decisions reports/eval --output-dir reports/models
.venv/bin/elliottwaver support-matrix --model-summary reports/models/summary.json
```

评估输出 `decisions.jsonl`、`events.parquet`、`summary.json` 和 `manifest.json`。周线样本可以从日线快照派生。先看 `summary.json` 的样本量、成熟比例和分组结果，需要核对单条判断时再看明细。

| 事件 | 衡量什么 |
| --- | --- |
| `CLOSE_BAND_5D_V1` / `CLOSE_BAND_2W_V1` | 期限末收盘价相对判断时的收盘价 ± 1 个 ATR，高于上界为 `UP`，低于下界为 `DOWN`，其余为 `RANGE` |
| `CONFIRM_FIRST_20D_V1` / `CONFIRM_FIRST_8W_V1` | 候选在 20 个交易日或 8 个交易周内，是否在失效前先满足确认条件 |
| `TARGET_FIRST_20D_V1` / `TARGET_FIRST_8W_V1` | 候选在同一期限内，是否在失效前先到达主目标区 |
| `ADVANCE_FIRST_5D_V1` / `ADVANCE_FIRST_2W_V1` | 候选在短期观察期内，是否在硬失效或阶段退出前满足推进条件 |
| `OUTLOOK_TARGET_FIRST_5D_V1` / `OUTLOOK_TARGET_FIRST_2W_V1` | 候选在短期观察期内，是否在硬失效或阶段退出前到达可观察的主目标区 |
| `ADVANCE_FIRST_{1M,5M,15M,30M,60M}_5B_V1` | 盘中候选在冻结的 5 根主周期 K 线内，是否先满足推进条件 |
| `OUTLOOK_TARGET_FIRST_{1M,5M,15M,30M,60M}_5B_V1` | 盘中候选在同一 5 根窗口内，是否先到达可观察目标区 |

盘中协议在 `samples[].source_timeframe` 写分钟周期，`grid.cadence` 使用 `EVERY_BAR`，`grid.step` 选择每隔多少根 K 线生成一个决策点。`events.parquet` 额外保留 `decision_at`、`due_at`、`period_starts`、`period_ends`、`trigger_bar_id`、`trigger_start_at` 和 `trigger_end_at`。评估、回放和结算使用同一个盘中观察窗解析器。

事件的期限、价格坐标和候选条件在决策时冻结。触价事件若在同一根 K 线上同时达到目标和失效边界，会标记 `AMBIGUOUS_SAME_BAR`。评估可以使用后来取得的冻结快照，`DECISION_TIME` 表示按模型批准时间选模，研究结果仍是冻结数据回放。

样本较多时，可以把原协议分别复制到多个文件，每份只改 `samples`，从同一份源码分别跑评估，然后按完整协议合并。合并会检查样本覆盖、配置和生成各批结果的源码指纹。

```bash
.venv/bin/python scripts/merge_evaluations.py \
  --protocol "$PROTOCOL" \
  --input reports/eval-part1 --input reports/eval-part2 \
  --output-dir reports/eval-merged
```

合并完成后，训练时使用 `--decisions reports/eval-merged`。

训练校验评估清单和文件哈希，按市场、证券类别、周期、feed 与复权口径分别生成模型，并按事件预定到期日剔除跨分段样本。单组模型在输出目录保存 `model.json`、`summary.json` 和 `manifest.json`。多组运行时，用顶层 `summary.json` 中各组的 `model_path` 找到具体模型。仓库协议的 `release` 字段记录以下发布门槛。

- 相对类别频率、20 根动量、完整价格组三项基线，Brier 和 log loss 改善的配对 95% 区间下界均大于 0；
- 每个展示可靠性档至少 50 个成熟观测和 10 个不同决策日期，且来自至少两个有样本的时间块。预测均值减实际发生率的 95% 区间须在 ±0.10 内。
- 测试样本至少 50 行、10 个日期、5 个标的；
- 支持档覆盖率至少 0.5。

训练结束后先看各事件的 `status` 和 `reasons`。仓库当前没有批准的真实行情模型，日常摘要保持未校准。合格的模型由维护者通过 `elliottwaver.forecast.ModelRegistry.register` 登记到缓存的 `models/` 子目录，须包含至少一个 `APPROVED` 事件，批准日须晚于留出期最后一天。训练目录中的模型文件应保留原路径，供注册表读取。

- `brief` 在默认缓存的 `models/` 中按决策时间查找兼容的已批准模型，并按事件和候选位置批量选择。不同事件可以由不同模型提供，同一事件的主选和备选也可分别注册；旧事件仍可回退到旧模型。可用 `--model-registry DIR` 指定其他注册表。显式 `brief --model /path/to/model.json` 会在加载成功后将摘要标记为回顾性重算。三分类事件中任一分量落入未获支持的档位，整个事件显示 `UNSUPPORTED_BIN`。
- 短期事件的历史评分使用 `data_status: HISTORICAL_HOLDOUT`，输出 `study_mode: RETROSPECTIVE` 和 `operational_allowed: false`。训练的 `freeze` 记录实际完成时刻，`--frozen-at` 可指定模拟的历史切分时刻。旧版候选仍可做历史评分。 [短期事件的实验和验证命令](protocols/README.md#短期事件的历史与前向评分)
- `evaluate_briefs.py --model-registry DIR` 使用决策时已批准且未撤回的模型。另可用 `--model /path/to/model.json` 重算兼容的历史决策，省略批准时间时全部标记为回顾性。如果确实有批准记录，可加带时区的 `--model-approved-at`，只把早于批准时间的决策标记为回顾性。批准日须晚于模型留出期末日。查看明细的 `model_id`、`model_approved_at`、`retrospective` 和汇总的 `model_usage`。

用 30 只沪深个股和 7 个指数重算 2026 年留出后，个股和指数各四项日线事件仍为 `INSUFFICIENT_EVIDENCE`。指数 20 日目标先达的价格基线对照与概率校准均未过。[留出重算与历史压力测试](reviews/2026-11-20-cn-index-probability-recheck.md)

指数目标先达的新校准候选已冻结。可用独立评分命令复用候选及对照参数，检查未来留出的发布门槛。[前向验证流程](protocols/cn-index-target-forward.md)

### 4.9 盲标导出与一致性评分

首版可直接用 AI 编写的独立端点规则复核。输入盲标 JSON 和同一缓存中的快照，输出每例的证据和通过状态。输入完整的日线可逐段核对影线，缺端点或缺证据保留待定。输出范围限于端点形态，子浪证据单列。

```bash
.venv/bin/python scripts/review_wave_cases.py \
  --cases reports/blind-cases.json --cache-root .cache/elliottwaver \
  --output reports/rule-review.json
```

下方人工标注流程可用于研究独立判读认可概率。

先用 `brief` 生成要评审的报告。下面从默认目录中的贵州茅台摘要导出一个可直接打开的静态标注页面。

```bash
.venv/bin/python scripts/label_wave_cases.py export \
  --briefs reports/CN/600519.SH/brief \
  --cache-root .cache/elliottwaver --bar-window 60 \
  --output reports/blind-cases.json --html reports/blind-cases.html
```

导出时校验报告清单和文件，跳过不完整或校验失败的报告。端点按出现顺序编号。页面覆盖「候选」「被拒绝的结构」与「无候选」三类，`--max-per-stratum` 可限制每层数量。源快照无法读取时，仍可导出端点并标记 `BARS_UNAVAILABLE`。要从自备案例 JSON 导出 JSON，可用 `export --input cases.json --output reports/blind-cases.json`。

1. 用浏览器打开 `reports/blind-cases.html`。每位标注者填写自己的 `annotator_id`，独立完成判断。
2. 每个案例可多选浪型、单独选方向，也可选择弃权。确认没有适用浪型时，可留空浪型并勾选「已完成本案例判断」。只有已完成的案例会进入下载的 CSV。
3. 各自下载 CSV。评分前合并 CSV 为 `reports/labels.csv`，只保留一行表头。同一个案例和标注者的组合仅允许一条记录。导入、评分与训练都会拒绝重复提交。

```bash
.venv/bin/python scripts/label_wave_cases.py import \
  --input reports/labels.csv --output reports/labels.json
.venv/bin/python scripts/label_wave_cases.py score \
  --labels reports/labels.json --cases reports/blind-cases.json \
  --output reports/labels-score.json
```

如需计算实际未来涨跌，可从案例引用的快照中取到期收盘价。完整结果单独存档。数据尚未覆盖到期时会标记 `RIGHT_CENSORED`。

```bash
.venv/bin/python scripts/label_wave_cases.py direction \
  --cases reports/blind-cases.json --cache-root .cache/elliottwaver \
  --horizon 5 --output reports/direction-labels.json
```

需要训练识别模型时，先冻结按案例分配的训练、校准和留出集。在合并后的 CSV 中新增 `split` 列，填 `train`、`calibration` 或 `test`，同一案例的全部标注必须使用同一值。重新执行 `import`，再运行训练和预测。

```bash
.venv/bin/python scripts/train_label_models.py \
  --cases reports/blind-cases.json --labels reports/labels.json \
  --output-dir reports/label-models

# 只对 APPROVED 模板预测
.venv/bin/python scripts/label_wave_cases.py predict \
  --cases reports/blind-cases.json --model reports/label-models/label-model.json \
  --output reports/label-predictions.json
```

手工编辑 CSV 时，沿用页面下载的字段 `case_id, annotator_id, selected_patterns, direction, abstain, reason`。`selected_patterns` 可用 `|` 分隔，`direction` 可填 `UP`、`DOWN` 或 `ABSTAIN`。核对 `labels-score.json` 中各模板的以下指标。

- `recognition_probability` 按案例均权汇总认可比例。
- `double_valid_cases`、`double_positive`、`double_negative`、`discordant_count/cases`。
- `positive_rate_left/right`、`annotation_positive_rate`、`case_positive_rate`。
- `raw_agreement`、`cohen_kappa`。
- `gate` 要求至少 50 个不同标注者双人有效案例、10 个双阳性、10 个双阴性，且 κ ≥ 0.60。未通过时 `eligible_for_calibration=false`。

`--cases` 可提供 case 清单，用于按 case 的 `split` 分组和统计 `missing_cases`；`score` 顶层输出 `by_split`、`abstain`、`empty_selection`、`cases_without_valid` 和 `cases_with_extra_annotations`。`directions` 单独报告标注者涨跌方向的一致率和 Cohen κ。

每个模板的识别模型估计独立标注者认可该浪型的概率。只有通过双人标注门槛和留出校准的 `APPROVED` 模板才会出现在 `predict` 输出中。当前真实标签模型尚未获批，收盘摘要中的价格事件概率另按 4.8 节评估。

## 5. 找到并查看报告

收盘摘要默认写入 `reports/<市场>/<标的>/brief/<行情日期>/<产物摘要>/`。终端打印目录。使用 `--json` 时，输出摘要对象，可通过 `history SYMBOL --json` 查询保存路径。

| 摘要文件 | 内容 |
| --- | --- |
| `index.html`、`report.md` | 默认生成的收盘摘要 |
| `daily.json`、`daily.html` | 主周期的完整结果和图表。主周期为周线时沿用同样的文件名，可看 JSON 的 `timeframe` 确认 |
| `weekly.json`、`weekly.html` | 周线背景结果和图表，仅在算出背景结果后生成 |
| `brief.json`、`manifest.json` | 摘要数据和产物校验清单，始终保存 |

`brief --format json` 只保存 JSON 产物。`--format html,md` 包含 HTML 和 Markdown，`--renderer svg` 可更换图表渲染器。同一个业务判断以 `brief_digest` 标识。导出格式或渲染器变化时，会使用不同产物目录。

`analyze` 和 `demo` 输出单周期报告。终端最后会给出类似信息。

```text
报告：reports/CN/600519.SH/1d/2026-11-16/<产物摘要>
快照：<快照ID>
```

默认真实分析写入 `reports/`，演示写入 `reports/demo/`。目录中的日期为最后一根纳入分析的 K 线所属交易日。指数报告使用相同结构，例如 `reports/CN/000688.SH/1d/2026-11-16/<产物摘要>/`，并在报告头部标注“品种：指数”。

```text
reports/
  CN/600519.SH/1d/2026-11-16/<产物摘要>/
    chart.html       交互图表
    report.md        文字报告与评分依据
    analysis.json    完整结构化结果
    manifest.json    文件校验值与输入/结果身份
```

HTML 包含 K 线、成交量、MACD 三个面板。可以通过图上方的下拉框切换候选，通过工具栏缩放、平移和重置视图。鼠标移到已确认拐点上，可查看极值时间与确认时间。

图中的蓝色折线表示已确认拐点，橙色虚线表示未确认的进行中端点，绿色点线表示条件性参考路径，绿色区域表示目标带。失效价为红色水平线，其他条件线为蓝灰色；具体比较条件以 `report.md` 或 JSON 为准。参考路径横轴按主周期 K 线根数表示，不是确定的日历日期。

指定输出根目录或导出格式：

```bash
.venv/bin/elliottwaver analyze 600519 --output-dir reports/my-scan --format html,md

# 直接生成 PNG，无需 Kaleido
.venv/bin/elliottwaver analyze 600519 --renderer svg --format html,png,md

# 完整 JSON 输出到终端，文件报告仍会生成
.venv/bin/elliottwaver analyze 600519 --format json --json
```

`analysis.json` 和 `manifest.json` 始终写入；`--format json` 表示只保留这两个基础文件。要额外导出 Plotly 的 PNG，需安装 `.[png]` 并满足 Kaleido 的浏览器要求；上面的 SVG 渲染命令可直接生成 PNG。

每个股票/周期目录下的 `latest_<配置摘要>_<feed>.json` 是最近一次有效报告的路径指针。打开图表时使用终端打印的具体目录即可。

## 6. 怎么理解分析结果

先看数据截止日期和总体状态，再比较候选，最后阅读条件及证据。

### 6.1 总体状态

| 状态 | 含义与处理方式 |
| --- | --- |
| `OK` | 存在达到当前评分门槛的候选；继续查看规则、覆盖度和失效条件 |
| `UNCLEAR` | 候选不够清晰，或没有达到门槛的候选；这是正常结果 |
| `SEARCH_LIMITED` | 搜索达到预算限制，候选枚举不完整；可检查配置和搜索诊断 |
| `INSUFFICIENT_DATA` | 数据根数不足，或分析范围内存在未知缺口；先检查输入数据 |
| `STALE_DATA` | 输入未覆盖请求截止时刻；更新行情，或明确使用旧快照 |

数据在分析前校验失败时，命令可能直接输出错误，不生成图表。报告已经生成也不等于数据状态为 `OK`，仍需查看状态字段。

### 6.2 候选、评分与阶段

| 字段 | 怎么读 |
| --- | --- |
| S1、S2、S3 | 本次展示的不同数浪候选，按评分排序 |
| 质量分 | 0–100，表示工程评分规则下的证据质量 |
| 证据覆盖 | 适用特征中已取得证据的比例；缺特征不会凭空提高质量 |
| 候选权重 | 已枚举、达标候选之间的相对评分权重，不是成功概率 |
| `omitted_weight` | 达标但未进入 Top-K 的候选权重；因此展示权重之和可能小于 100% |
| `ambiguity` | 结构歧义，`LOW / MEDIUM / HIGH` 表示当前候选之间的区分程度 |
| `W3_DEVELOPING` 等 | 当前处于第 3 浪等进行中阶段，最后端点尚未确认 |
| `B_DEVELOPING / C_DEVELOPING` | ABC 模板中的 B 浪或 C 浪进行中 |
| `COMPLETE` | 当前模板已完成；查看 `next_leg_bias` 判断程序评估的下一段方向 |
| 浪级 | `MINOR / INTERMEDIATE / PRIMARY / CYCLE / SUPERCYCLE`；按候选 K 线跨度启发式划分，不同浪级不宜直接比较 |
| 父级上下文 | `VERIFIED` 要求候选与更高浪级的已确认父腿端点吻合，方向一致，且子模板符合该浪段的规则。`PROVISIONAL` 表示仅有时间包含关系、端点仍在形成，或父模板的内部规则待确定。`UNKNOWN` 表示本次枚举未找到符合条件的关系。 |
| 父级关系 | `EXACT_LEG` 表示两个端点的时间、价格和高低类型均对齐。`CONTAINED` 表示候选的时间区间位于父腿内部。`UNKNOWN` 表示没有父级关系。 |
| 子浪状态 | `VERIFIED` 表示所有父腿都已确认并通过位置和子模板硬规则。`UNVERIFIED` 表示认证未完成。`UNAVAILABLE` 表示缺少适用规则或足够的细尺度拐点。 |

当前枚举普通推动浪、ABC 锯齿、三角形、平台、楔形和双锯齿 WXY；新模板先按端点级硬规则筛选，再用可选子浪认证检查内部结构。复杂组合调整仍未覆盖。`pattern_direction` 是整个模板方向，`next_leg_bias` 是下一段方向，二者在完成态可能不同。

推动浪与楔形按各浪自身起点计算浪 1、3、5 的百分比长度。浪 3 短于浪 1 时，进行中的浪 5 按同一口径显示失效价。`ew-rules-v5` 在计算和展示时共用同一个精确触线价格。上涨看最高价、下跌看最低价，触线有效，严格越界才失效。完成三角形的 E 浪须形成净回撤。顺势三角形允许 B 浪越过 A 浪起点。 [规则与复验](reviews/2026-11-20-elliott-rule-conformance.md#修复复验) · [分价边界复验](reviews/2026-11-21-wave-engine-followup-repair.md)

### 6.3 条件、目标与证据

`report.md` 中会列出以下项目：

| 项目 | 含义 |
| --- | --- |
| `INVALIDATION` | 失效条件；触发后当前结构假设不再成立 |
| `CONFIRMATION` | 确认条件；达到指定价格条件后取得新的确认依据 |
| `STAGE_EXIT` | 当前阶段的退出边界 |
| `target_zones` | 条件性目标价格区间，附计算方法与独立方法数 |
| `EXCLUDED_STRUCTURE` | 硬规则通过、证据覆盖达标，但质量分未达门槛的结构；列在 warnings 中，不参与候选权重归一化 |
| 规则 `PASS / PENDING` | 当前约束未被证伪 / 尚待后续行情确认 |
| 证据 `UNAVAILABLE` | 尚未取得该项证据；不能按已验证处理 |

条件中的 `HIGH`、`LOW`、`CLOSE` 分别指最高价、最低价、收盘价，`< / <= / > / >=` 指明比较方式。例如 `LOW < 100` 表示最低价低于 100 时触发。

`COMPLETE` 模板的目标区使用 38.2%、50%、61.8%、78.6% 和 100% 回撤位；如果价格已经越过 61.8%，仍会给出更深的条件性目标，而不是直接显示无目标。

默认的 `ew_score_v2` 评分中，第五浪的 Fibonacci 证据同时比较浪 5/浪 1 与浪 5/浪 3，并纳入 1.618、2.618、4.236 等扩展位；两种关系取较优拟合，不要求同时满足。这是新的工程评分口径，不能据此认定预测效果提高。

如需原第五浪评分，在 YAML 中配置 `scoring.profile: ew_score_v1`：只比较浪 5/浪 1，核心比例为 1.0/0.618，次优比例 1.618 的权重为 0.7，sigma 为 0.10。它保留旧评分公式，其他目标推演和报告仍使用当前引擎。报告底部的引擎标识和 JSON 的 `engine_version` 会追加当前 profile，例如 `.../0.1.0/ew_score_v2`；切换 profile 或升级引擎后，配置/结果摘要会相应变化，不应期待旧结果 ID 保持一致。

浪级是启发式：日线跨度约 `<=60 / <=250 / <=1000 / <=2500` 根对应 `MINOR / INTERMEDIATE / PRIMARY / CYCLE`，更长为 `SUPERCYCLE`；周线约 `<=13 / <=52 / <=260 / <=520`。不同浪级的候选同时出现时，报告会给出 `MIXED_DEGREE_SCENARIOS`，应优先在同一浪级内比较。

三角形通过 A–C 与 B–D 轨道的斜率和宽度判断收敛或扩散。楔形采用收敛、非截短的端点模板，检查第 2 和第 4 浪的回撤边界、第 1 和第 4 浪重叠及第 3 浪长度。平台 B 至少回撤 A 的 90%，可越过 A 起点，C 须回到 A 端点附近。双锯齿 X 至少回撤 W 的 50% 且不能触及 W 起点，Y 完成时须越过 W 端点。这些规则用于筛选端点形态候选。

已完成收敛三角形使用经典 A 组规则。整体 `VERIFIED` 要求至少四条腿是已验证的锯齿族结构，复杂腿最多一条。首组选法失败时，程序会检查各腿在支持尺度上找到的备选组合。E 腿嵌套三角形显示为 `PROVISIONAL` 候选提示。[规则选择和复验](reviews/2026-11-18-cn-triangle-classic-v6.md)

`--subwave-depth 1/2` 启用子浪规则认证。程序用父腿端点和更细尺度的已确认拐点组成子候选，按父模板和浪段选择允许的子模板，再检查硬规则。普通推动浪第 3 浪只允许 `IMPULSE`。通用 `DIAGONAL` 端点模板的内部结构保留为 `UNAVAILABLE`，直到能够区分引导和终结类型。深度 2 会在已认证的子结构上再向下检查一层，缺少下一层证据时保留深度 1 结果。`--long-term` 默认深度 1，显式传 `--subwave-depth 0` 可关闭检查。

目标带、条件价和图中分析价格属于同一复权坐标；原始收盘价另存为 JSON 的 `latest_raw_close`。比较不同历史截止日期的 A 股结果时，还应核对复权基准和 `price_coordinate_id`。

父级上下文只使用本次枚举到的候选建立关系，搜索未枚举到的更大结构仍为 `UNKNOWN`。子浪认证覆盖当前实现的 5 浪推动/楔形和 3 浪锯齿/平台，复杂组合调整、running flat、扩散楔形和截短五浪仍未覆盖。单周期分析中的候选字段 `calibrated_event_probability` 保持 `null`。通过发布门槛的事件概率由 `brief.json` 的 `forecasts` 单独提供。

## 7. 日期、时间和锚点

不指定日期时，按当前时间选择已经结束并达到可用时间的 K 线。默认可用时间缓冲为 20 分钟，未完成 K 线不会纳入。

```bash
# 以某个已完成交易日为截止时刻
.venv/bin/elliottwaver analyze 600519 --as-of 2026-11-16

# 分钟分析，必须携带时区
.venv/bin/elliottwaver analyze 600519 --timeframe 5m --as-of '2026-11-16T14:00:00+08:00'

# 限定候选锚点搜索的最早日期，以及数据截止日期
.venv/bin/elliottwaver analyze 600519 --start 2025-01-01 --end 2026-11-16
```

`--as-of YYYY-MM-DD` 对 A 股解释为该日收盘后加缓冲，对美股解释为纽约当地次日零点加缓冲。精确时间必须带 `+08:00`、`-05:00`、`Z` 等时区信息；不能请求未来尚不可用的时刻。

`--end` 限制纳入的交易日期；`--as-of` 控制数据当时是否已经可用。`--start` 影响锚点搜索范围，程序仍可保留更早数据用于指标预热和图表展示，因此不应把它当成严格的下载或显示起点。

高级用法：`--anchor YYYY-MM-DD` 将起浪日期限定为该日。该日期必须有可用 K 线；如果没有匹配的合法当前计数，会出现 `EXPLICIT_ANCHOR_UNMATCHED`。盘中还可传入带时区的精确开始时刻，匹配 K 线的 `start_at`：

```bash
.venv/bin/elliottwaver analyze AAPL --timeframe 5m --anchor '2024-01-05T13:35:00-05:00'
```

指定时刻对应的 K 线必须已可用；否则返回明确的锚点错误。日期锚点仍按交易日期匹配，精确时间锚点不会退化为同日任意一根 K 线。

用现在下载的历史行情运行上述命令，结果属于冻结数据回放。供应商历史数据与复权因子可能已经修订，不能据此声称复现了当时真实可取得的数据版本。

## 8. 保存快照与离线分析

正常分析会自动保存快照。也可以先下载数据，再决定如何分析：

```bash
.venv/bin/elliottwaver data fetch 600519 --timeframe 1d
```

命令输出 JSON，其中 `snapshot_id` 是该快照的完整 ID。将其填入下面的变量，保留引号：

```bash
SNAPSHOT_ID='替换为刚才返回的完整snapshot_id'

.venv/bin/elliottwaver data inspect "$SNAPSHOT_ID"
.venv/bin/elliottwaver brief 600519 --offline --snapshot "$SNAPSHOT_ID"
.venv/bin/elliottwaver analyze 600519 --offline --snapshot "$SNAPSHOT_ID"

# 日线快照也可以生成完整周线分析
.venv/bin/elliottwaver analyze 600519 --offline --snapshot "$SNAPSHOT_ID" --timeframe 1w
```

默认快照目录为 `.cache/elliottwaver/`。快照文件及元数据会校验 SHA256，不要直接修改其中的文件。

`analyze --offline` 默认沿用快照周期和截止时间。`brief` 在明确给出盘中快照时自动采用该快照的主周期；没有盘中快照时仍按日线/周线自动选择。标的和 feed 必须匹配快照。日线快照可以派生周线；盘中背景按固定配对从同一底层行情派生，主分析需要对应周期的快照。

若通过配置修改了缓存目录，取数、分析、历史查询和回放时都传同一份 `--config`。验收时使用的 `.cache/live-smoke/` 与默认缓存是两个目录。

显式请求超过快照覆盖的时刻会报 `STALE_DATA`。确需查看旧数据时可添加 `--allow-stale`，结果仍会标记陈旧；该参数不会延长行情覆盖或消除缺口。

## 9. 历史回放

沿用上一节设置的日线 `SNAPSHOT_ID`：

```bash
.venv/bin/elliottwaver replay "$SNAPSHOT_ID" \
  --start 2025-01-01 \
  --end 2026-11-16 \
  --step 5 \
  --output reports/replay-600519.jsonl
```

回放在每个选中时点，仅向同一个分析引擎提供当时已可用的 K 线前缀。至少需要 60 根历史 K 线。`--step 5` 表示每隔 5 根符合日期条件的主周期 K 线计算一次，周线和分钟线也按主周期根数计数。回放默认严格检查缺口。复现使用 `--allow-gaps` 的分析时，给 `replay` 也传 `--allow-gaps`，并使用与分析相同的搜索配置和主周期快照。

输出两个文件：

- `reports/replay-600519.jsonl`：每行一份完整分析结果。
- `reports/replay-600519.jsonl.manifest.json` 记录源快照、引擎、配置、日期范围、步长、缺口策略与文件校验值。

`replay` 用于观察计数随时间变化和验证前缀计算，模式为 `FROZEN_DATA_REPLAY`。如需按固定交易会话核对收盘、确认和触价结果，使用[离线评估流程](#48-离线评估与概率研究)。

## 10. 配置与批量处理

配置文件是可选的，不传时使用内置默认值。可以复制并编辑样例：

```bash
cp -n config.example.yaml config.yaml
.venv/bin/elliottwaver analyze 600519 --config config.yaml
```

常用设置：

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `analysis.timeframe` | `1d` | 默认主周期 |
| `analysis.top_k` | `3` | 默认展示候选数 |
| `analysis.history_years` | `5` | 请求历史年数；分钟历史仍受供应商限制 |
| `analysis.max_analysis_bars` | `600` | 锚点搜索窗口；长历史可提高到 5000 |
| `analysis.max_anchors` | `4` | 最多锚点数；长历史预设为 8 |
| `analysis.anchor_mode` | `recent` | `recent` 取最近锚点；`spread` 首尾兼顾 |
| `analysis.scales` | `[1.4, 2.4, 3.8]` | 多尺度 ATR ZigZag 的阈值 |
| `scoring.profile` | `ew_score_v2` | v2 扩展第五浪评分；可选 `ew_score_v1` 保留原公式 |
| `search.beam_width` | `128` | 搜索束宽；大级别模式 512 |
| `search.max_expansions` | `50000` | 有限搜索的扩展预算；大级别模式 200000 |
| `search.max_bridge_intervals` | `5` | 允许跳过的最大间隔（奇数 1–21）；大级别模式 21 |
| `search.max_subwave_depth` | `0` | `0/1/2`；1 认证一层子浪，2 再向下认证一层 |
| `data.us_feed` | `sip` | 默认美股 feed |
| `data.delayed_access_buffer_minutes` | `20` | K 线结束后的可用时间缓冲 |
| `cache.directory` | `.cache/elliottwaver` | 快照、摘要历史、结算记录和默认模型注册表的根目录 |
| `output.directory` | `reports` | 真实分析默认报告根目录 |
| `output.formats` | `[html, md]` | 默认报告格式；JSON 始终保存 |

配置字段必须符合样例结构，未知字段会报错。`search.max_subwave_depth` 允许 `0/1/2`；`1` 对已确认父腿做一层子模板规则认证，`2` 再向下认证一层。`--long-term` 会把它设为 1，显式 `--subwave-depth 0` 可覆盖。`demo` 有独立的默认输出目录和格式，需要通过其 `--output-dir`、`--format` 修改。

已暴露为命令参数的设置，其优先级为：显式命令参数 > `ELLIOTTWAVER_` 环境变量 > YAML 配置 > 内置默认值。例如只对一次调用改变候选数：

```bash
ELLIOTTWAVER_ANALYSIS__TOP_K=5 .venv/bin/elliottwaver analyze 600519
```

需要自己的脚本读取结果时，使用 `--json`，诊断和错误会走 stderr：

```bash
mkdir -p reports
.venv/bin/elliottwaver analyze 600519 --format json --json > reports/600519-result.json
```

批量收盘摘要使用 [`watchlist`](#47-自选批量)。如果需要为每只标的生成单周期图表，可逐只调用 `analyze`。例如依次分析 3 只股票，失败时输出股票代码并继续：

```bash
for symbol in 600519 000001 300750; do
  if ! .venv/bin/elliottwaver analyze "$symbol" --timeframe 1d; then
    printf '分析未成功：%s\n' "$symbol" >&2
  fi
done
```

## 11. 常用参数速查

以下为 `analyze` 的参数；其他命令支持的参数以各自 `--help` 为准。

| 参数 | 用途 |
| --- | --- |
| `--timeframe 1h` | 主分析周期；默认日线，离线时默认快照周期 |
| `--top-k 5` | 最多展示多少个达标候选 |
| `--as-of ...` | 指定可用信息截止时刻 |
| `--start / --end` | 锚点搜索下界 / 纳入行情的交易日期上界 |
| `--anchor ...` | 指定起浪日期 |
| `--feed sip` | 美股 feed；A 股不接受该参数 |
| `--refresh` | 跳过缓存复用，从行情源重新获取 |
| `--snapshot ID --offline` | 使用已有快照，不联网 |
| `--allow-stale` | 显式接受快照覆盖不足，保留陈旧标记 |
| `--allow-gaps` | 长历史个股停牌/未知缺口继续分析，输出 `ALLOWED_GAPS` |
| `--long-term` | 20 年窗口、周线默认、Top-8、首尾锚点、大级别搜索、深度 1 子浪认证 |
| `--large-degree` | 只放宽桥接/beam/扩展预算，不改变数据窗口 |
| `--subwave-depth 0/1/2` | 子浪规则认证深度；0 关闭，1 认证一层，2 再向下认证一层 |
| `--output-dir PATH` | 报告根目录 |
| `--format html,png,md` | 附加产物格式，JSON 和 manifest 始终保存 |
| `--renderer plotly` | `plotly` 为默认交互图；`svg` 为另一渲染器，可直接导出 PNG |
| `--json` | 将完整结果输出至 stdout |
| `--config PATH` | 读取 YAML 配置 |
| `--open` | `analyze` 尝试打开 `chart.html`，`brief` 尝试打开 `index.html` |

```bash
.venv/bin/elliottwaver --help
.venv/bin/elliottwaver analyze --help
.venv/bin/elliottwaver brief --help
.venv/bin/elliottwaver history --help
.venv/bin/elliottwaver watchlist --help
.venv/bin/elliottwaver settle --help
.venv/bin/elliottwaver support-matrix --help
.venv/bin/elliottwaver demo --help
.venv/bin/elliottwaver data fetch --help
.venv/bin/elliottwaver data inspect --help
.venv/bin/elliottwaver replay --help
```

## 12. 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `elliottwaver: command not found` | 使用 `.venv/bin/elliottwaver`，或先 `source .venv/bin/activate` |
| 缺少 Python 模块 | 检查使用的是项目虚拟环境，运行 `doctor`；按安装步骤补齐依赖 |
| 网络解析失败、超时、`SOURCE_UNAVAILABLE` | 在运行该命令的终端检查网络和代理；离线演示及已有快照分析不需要网络 |
| Alpaca 401/403 或权限错误 | 检查密钥、paper/live 端点以及显式选择的 feed 权限；`doctor` 不验证在线权限 |
| `UNCLEAR` 或候选表为空 | 当前没有足够清晰的合法候选；可以对比其他周期及更多历史，不需要把它当作程序崩溃 |
| `At least 60 completed bars` | 已完成数据少于 60 根；换用有足够历史的周期或快照 |
| `UNKNOWN_SESSION_GAPS` | 分析范围存在未知缺口；检查停牌/源数据，程序不会自动补造价格 |
| `ANCHOR_FALLBACK` | 没有找到合格背景周期锚点，改用明确标记的回退窗口；不代表下载失败 |
| `EXPLICIT_ANCHOR_UNMATCHED` | 人工指定日期没有匹配合法当前计数；检查日期，或去掉 `--anchor` 使用自动锚点 |
| `STALE_DATA` | 检查请求时间与快照覆盖；更新数据或显式使用旧快照 |
| `INDEX_INTRADAY_UNSUPPORTED` | 指数目前只支持 `1d` / `1w`；分钟线尚未启用，改用日线或周线 |
| `CALENDAR_CLIPPED` | 请求起点早于 XSHG 日历 1991-01-02；更早的原始数据被裁剪 |
| `DATA_START_CLIPPED` | 请求的 `--start` 早于实际第一根 K 线；检查数据源覆盖 |
| `ANALYSIS_WINDOW_TRUNCATED` | 数据根数超过 `max_analysis_bars`，锚点只用最近 N 根；提高该值或改用周线 |
| `ALLOWED_GAPS` | 显式 `--allow-gaps` 后忽略了停牌/未知缺口；结果保留该警告 |
| `MIXED_DEGREE_SCENARIOS` | 同时展示了不同浪级的候选；先在同一浪级内比较 |
| `PARENT_CONTEXT_PROVISIONAL` | 当前只看到时间包含关系，端点仍在形成，或缺少父模板的内部规则。具体原因见候选说明。 |
| 子浪状态 `UNVERIFIED` | 部分父腿子模板未通过，或父级最后一条腿仍在进行中；查看 `subwave_evidence` |
| 子浪状态 `UNAVAILABLE` | 缺少适用的内部规则或足够的更细尺度确认拐点。 |
| 输入 `000688` 被当成深市个股或身份失败 | 上证指数要写 `000688.SH` 或 `sh000688`；`000001` / `000001.SZ` 默认是平安银行 |
| `SH880xxx sector indices are not supported` | 同花顺行业指数当前未接入；请使用 `SH000*`、`SZ399*`、`BJ899*` 官方指数代码 |
| 快照不存在 | 检查完整 ID、当前工作目录，以及是否使用相同 `cache.directory` |
| 快照周期不匹配 | 使用快照原周期，或从日线派生周线。小时线需单独获取，`brief` 读取分钟快照时须显式指定对应周期 |
| 批量报告没有 HTML | `watchlist` 保存 JSON 产物。单独运行 `brief SYMBOL` 生成可读摘要和图表 |
| 概率显示 `UNCALIBRATED` | 当前分析未使用适用的已批准模型，可以继续看浪型、权重和确认/失效条件 |
| 结算显示 `RIGHT_CENSORED` | 事件所需的后续交易会话尚未被快照覆盖。到期后用对应标的的新快照重新结算 |
| 1 分钟线每天少两个时隙 | 新浪现代沪深股票 profile 对 14:57、14:58 起始的无成交集合竞价时隙有明确处理；其余未知缺口仍会阻断分析 |
| PNG 导出失败 | 使用 `--renderer svg --format html,png,md`，或安装并配置 Plotly/Kaleido 的完整运行条件 |
| HTML 没有自动打开 | `demo` 不支持 `--open`；无桌面环境时手动下载并打开 HTML |

常用退出码：`0` 表示命令正常结束，`2` 表示输入或配置错误，`3` 表示数据不足/陈旧，`4` 表示来源或权限失败，`6` 表示报告导出失败。`UNCLEAR`、`SEARCH_LIMITED` 可以正常结束，因此自动化脚本还应读取 JSON 的 `status`，不能只看退出码。

当前交付范围和真实数据证据见[实现与验收状态](IMPLEMENTATION_STATUS.md)；算法与后续开发设计见[详细实现方案](IMPLEMENTATION_PLAN.md)。
