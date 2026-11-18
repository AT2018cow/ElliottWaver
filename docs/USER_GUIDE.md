# EliotBot 中文使用手册

适用版本：0.1.0 CLI。更新日期：2026-11-17。

EliotBot 从股票与指数 K 线中寻找普通推动浪和 ABC 锯齿候选，输出规则检查、评分、关键价位及图表。当前入口是终端命令，图表通过浏览器查看。

目录：

- [1. 先运行起来](#1-先运行起来)
- [2. 安装与环境检查](#2-安装与环境检查)
- [3. 股票代码与 K 线周期](#3-股票代码与-k-线周期)
- [4. 分析 A 股与美股](#4-分析-a-股与美股)
- [5. 找到并查看报告](#5-找到并查看报告)
- [6. 怎么理解分析结果](#6-怎么理解分析结果)
- [7. 日期、时间和锚点](#7-日期时间和锚点)
- [8. 保存快照与离线分析](#8-保存快照与离线分析)
- [9. 历史回放](#9-历史回放)
- [10. 配置与批量处理](#10-配置与批量处理)
- [11. 常用参数速查](#11-常用参数速查)
- [12. 常见问题](#12-常见问题)

## 1. 先运行起来

当前工作区已经安装依赖，可以直接执行下面的命令。以下示例均以项目根目录为工作目录：

```bash
cd <REPO_ROOT>

# 不联网、不需要密钥，先熟悉输出
.venv/bin/eliot demo --symbol 600519 --timeframe 1d

# 分析真实 A 股日线，完成后尝试打开浏览器
.venv/bin/eliot analyze 600519 --timeframe 1d --open
```

演示使用明确标记的合成行情；第二条命令使用新浪真实行情，需要联网。命令结束后，终端会打印报告目录与快照 ID。

如果运行环境没有桌面浏览器，进入报告目录，双击或下载其中的 `chart.html`，在自己的浏览器中打开。该 HTML 已包含所需脚本，不需要启动 Web 服务。

## 2. 安装与环境检查

新机器需要 Python 3.11 或更高版本。验证环境为 Linux / Python 3.12。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,cn,us]'
.venv/bin/eliot doctor --market CN
```

如需使用本项目记录的依赖版本：

```bash
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

锁定文件记录本次 Python 3.12 / Linux 环境的完整安装版本；其他系统仍需满足相应包的安装条件。

`doctor` 会列出依赖版本、支持的周期、浪型，以及是否配置 Alpaca 凭证：

| 字段或现象 | 含义 |
| --- | --- |
| `alpaca_credentials_configured: false` | 未配置美股凭证；不影响 A 股和离线演示 |
| `kaleido: null` | 未安装 Plotly 的 PNG 导出依赖；不影响 HTML、Markdown 或 SVG 渲染器的 PNG |
| `network_smoke: NOT_RUN` | 该命令没有测试网络，也没有验证真实行情权限 |
| `index_timeframes: ["1d","1w"]`；`bj_live: INDEX_ONLY` | 指数只支持日线/周线；北交所仅指数接入，个股未启用 |

不想每次输入 `.venv/bin/` 时，可以在当前终端激活环境：

```bash
source .venv/bin/activate
eliot analyze 600519 --timeframe 1d
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

`SZ000*` 仍是深市个股，`SH688*` / `SH689*` 仍是科创板个股。`SH880*` 同花顺行业指数当前不接入。指数目前只支持日线和周线；指数分钟线尚未启用。

当前线上范围为沪深 A 股、沪深京指数与美股个股；北交所个股尚未接通，港股尚未实现。

| 周期 | 参数 | 示例 |
| --- | --- | --- |
| 日线 | `1d`，默认值 | `eliot analyze 600519 --timeframe 1d` |
| 周线 | `1w` | `eliot analyze 600519 --timeframe 1w` |
| 小时线 | `60m` 或 `1h` | `eliot analyze 600519 --timeframe 1h` |
| 30 分钟 | `30m` | `eliot analyze 600519 --timeframe 30m` |
| 15 分钟 | `15m` | `eliot analyze 600519 --timeframe 15m` |
| 5 分钟 | `5m` | `eliot analyze 600519 --timeframe 5m` |
| 1 分钟 | `1m` | `eliot analyze 600519 --timeframe 1m` |

表格中的短命令需要先激活虚拟环境。月线和 4 小时线不能作为当前主分析周期。指数只支持 `1d` 和 `1w`；请求指数分钟线会返回 `INDEX_INTRADAY_UNSUPPORTED`。

每次调用分析一个主周期。A 股小时线不跨午休；美股分钟/小时线从常规交易时段（RTH）开盘对齐，收市前可能保留不足整小时的尾段。周线先对日线进行复权，再聚合完整交易周，尚未结束的周不会纳入。

分钟历史长度由供应商实际返回范围决定。增加 `history_years` 或指定更早的 `--start`，不能保证取到供应商未提供的分钟数据。

## 4. 分析 A 股与美股

### 4.1 沪深 A 股

无需 API key。日线、周线、小时线和分钟线使用同一命令：

```bash
.venv/bin/eliot analyze 600519
.venv/bin/eliot analyze 000001 --timeframe 1w
.venv/bin/eliot analyze 300750 --timeframe 1h
.venv/bin/eliot analyze 600519 --timeframe 15m --top-k 5
```

默认显示最多 3 个达到门槛的候选，`--top-k` 可以设为 1–20；增加这个值不会强行生成候选。

希望重新从行情源获取数据时使用：

```bash
.venv/bin/eliot analyze 600519 --timeframe 1d --refresh
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
.venv/bin/eliot doctor --market US
.venv/bin/eliot analyze AAPL --feed sip
.venv/bin/eliot analyze MSFT --timeframe 1h --feed sip
.venv/bin/eliot analyze AAPL --timeframe 15m --feed iex
```

默认使用 `sip`；`iex` 必须显式指定，或通过配置修改。需要账户具有所选 feed 的访问权限，权限失败会报错，不会自动更换 feed。不同 feed 的结果会标记来源并分别缓存。

美股原生日线按供应商日线口径处理：程序保守等到纽约当地次日零点再加访问缓冲后，才纳入该日线。分钟/小时线采用 RTH 口径，两者的数据截止时刻可能不同。

当前美股适配器已通过冻结响应测试，尚未完成真实账户联调，详见[实现与验收状态](IMPLEMENTATION_STATUS.md)。

### 4.3 沪深京指数

无需 API key。指数使用新浪指数日线，来源标记为 `sina-index`，调整口径为 `none`，不计算复权因子。目前支持日线和周线：

```bash
.venv/bin/eliot analyze 000688.SH --timeframe 1d
.venv/bin/eliot analyze 000300.SH --timeframe 1w
.venv/bin/eliot analyze 399001.SZ --timeframe 1d
.venv/bin/eliot analyze 899050.BJ --timeframe 1d
```

指数报告会显示“品种：指数；指数无复权，价格为原始点位”。`000001` 默认仍是平安银行；要分析上证指数请写 `000001.SH`。

指数默认只取最近 `history_years`（默认 5 年）；分析更长历史时设置 `analysis.history_years` 或 `--start`。例如：

```bash
.venv/bin/eliot analyze 000688.SH --timeframe 1w --start 2020-01-01
```

长期数浪还需要按需提高 `analysis.max_analysis_bars`（默认 600，最大 5000），否则锚点搜索只覆盖最近约 600 根 K 线。指数分钟线尚未启用；请求 `5m` 等周期会返回 `INDEX_INTRADAY_UNSUPPORTED`。

### 4.4 长历史与大级别搜索

A 股老指数/老股票原始数据可到 1990/1991 年；引擎日历从 **1991-01-02** 起，更早的数据会被裁剪并给出 `CALENDAR_CLIPPED`。默认只下载 5 年、锚点只看最近 600 根日线，所以长历史需要显式开启：

```bash
# 20 年窗口、默认周线、Top-8、首尾兼顾的锚点、宽桥接搜索
.venv/bin/eliot analyze 000001.SH --long-term --start 1991-01-01 --allow-gaps

# 只放宽搜索，不改变数据窗口
.venv/bin/eliot analyze 000300.SH --large-degree --timeframe 1w

# 子浪规则认证（0/1/2）
.venv/bin/eliot analyze 000001.SH --long-term --subwave-depth 1
```

`--long-term` 等价于：`history_years=20`、`max_analysis_bars=5000`、默认 `1w`、`top_k=8`、`anchor_mode=spread`、`max_bridge_intervals=21`、`beam_width=512`、`max_expansions=200000`、`max_subwave_depth=1`。`--large-degree` 只应用后三项搜索参数，不改变子浪深度。

30 年以上日线会超过 `max_analysis_bars` 上限 5000；请用周线，或接受 `ANALYSIS_WINDOW_TRUNCATED` 警告。个股长历史通常有停牌缺口；`--allow-gaps` 会继续分析并给出 `ALLOWED_GAPS` 警告，不补造价格。

## 5. 找到并查看报告

终端最后会给出类似信息：

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
.venv/bin/eliot analyze 600519 --output-dir reports/my-scan --format html,md

# 直接生成 PNG，无需 Kaleido
.venv/bin/eliot analyze 600519 --renderer svg --format html,png,md

# 完整 JSON 输出到终端，文件报告仍会生成
.venv/bin/eliot analyze 600519 --format json --json
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

父级上下文只使用本次枚举到的候选建立关系，搜索未枚举到的更大结构不会凭空补造，仍为 `UNKNOWN`。子浪认证覆盖当前实现的 5 浪推动/楔形和 3 浪锯齿/平台，复杂组合调整、running flat、扩散楔形和截短五浪仍未覆盖。`calibrated_event_probability` 保持 `null`；规则通过表示当前端点、影线和子模板约束通过，不等于所有外部父级位置和复杂内部结构都已被认证。

## 7. 日期、时间和锚点

不指定日期时，按当前时间选择已经结束并达到可用时间的 K 线。默认可用时间缓冲为 20 分钟，未完成 K 线不会纳入。

```bash
# 以某个已完成交易日为截止时刻
.venv/bin/eliot analyze 600519 --as-of 2026-11-16

# 分钟分析，必须携带时区
.venv/bin/eliot analyze 600519 --timeframe 5m --as-of '2026-11-16T14:00:00+08:00'

# 限定候选锚点搜索的最早日期，以及数据截止日期
.venv/bin/eliot analyze 600519 --start 2025-01-01 --end 2026-11-16
```

`--as-of YYYY-MM-DD` 对 A 股解释为该日收盘后加缓冲，对美股解释为纽约当地次日零点加缓冲。精确时间必须带 `+08:00`、`-05:00`、`Z` 等时区信息；不能请求未来尚不可用的时刻。

`--end` 限制纳入的交易日期；`--as-of` 控制数据当时是否已经可用。`--start` 影响锚点搜索范围，程序仍可保留更早数据用于指标预热和图表展示，因此不应把它当成严格的下载或显示起点。

高级用法：`--anchor YYYY-MM-DD` 将起浪日期限定为该日。该日期必须有可用 K 线；如果没有匹配的合法当前计数，会出现 `EXPLICIT_ANCHOR_UNMATCHED`。分钟周期当前也只接受日期锚点，不能指定某一分钟。

用现在下载的历史行情运行上述命令，结果属于冻结数据回放。供应商历史数据与复权因子可能已经修订，不能据此声称复现了当时真实可取得的数据版本。

## 8. 保存快照与离线分析

正常分析会自动保存快照。也可以先下载数据，再决定如何分析：

```bash
.venv/bin/eliot data fetch 600519 --timeframe 1d
```

命令输出 JSON，其中 `snapshot_id` 是该快照的完整 ID。将其填入下面的变量，保留引号：

```bash
SNAPSHOT_ID='替换为刚才返回的完整snapshot_id'

.venv/bin/eliot data inspect "$SNAPSHOT_ID"
.venv/bin/eliot analyze 600519 --offline --snapshot "$SNAPSHOT_ID"

# 日线快照也可以生成完整周线分析
.venv/bin/eliot analyze 600519 --offline --snapshot "$SNAPSHOT_ID" --timeframe 1w
```

默认快照目录为 `.cache/eliotbot/`。快照文件及元数据会校验 SHA256，不要直接修改其中的文件。

离线分析默认沿用快照周期和快照截止时间，不会因为今天的日期更晚就把快照当成新行情。股票、feed、周期必须与快照匹配；当前只额外支持日线 → 周线，不能拿日线快照分析小时线。

若通过配置修改了缓存目录，`fetch`、`inspect`、`analyze`、`replay` 都应传同一份 `--config`。验收时使用的 `.cache/live-smoke/` 与默认缓存是两个目录。

显式请求超过快照覆盖的时刻会报 `STALE_DATA`。确需查看旧数据时可添加 `--allow-stale`，结果仍会标记陈旧；该参数不会延长行情覆盖或消除缺口。

## 9. 历史回放

沿用上一节设置的日线 `SNAPSHOT_ID`：

```bash
.venv/bin/eliot replay "$SNAPSHOT_ID" \
  --start 2025-01-01 \
  --end 2026-11-16 \
  --step 5 \
  --output reports/replay-600519.jsonl
```

回放在每个选中时点，仅向同一个分析引擎提供当时已可用的 K 线前缀。至少需要 60 根历史 K 线。`--step 5` 表示每隔 5 根符合日期条件的主周期 K 线计算一次，周线和分钟线也按主周期根数计数。回放默认严格检查缺口。复现使用 `--allow-gaps` 的分析时，给 `replay` 也传 `--allow-gaps`，并使用与分析相同的搜索配置和主周期快照。

输出两个文件：

- `reports/replay-600519.jsonl`：每行一份完整分析结果。
- `reports/replay-600519.jsonl.manifest.json` 记录源快照、引擎、配置、日期范围、步长、缺口策略与文件校验值。

回放不会为每一行单独生成 HTML，也不计算交易收益。它用于观察计数随时间的变化和验证前缀计算，模式明确标记为 `FROZEN_DATA_REPLAY`；当前没有认证的 PIT 回放或胜率统计。

## 10. 配置与批量处理

配置文件是可选的，不传时使用内置默认值。可以复制并编辑样例：

```bash
cp -n config.example.yaml config.yaml
.venv/bin/eliot analyze 600519 --config config.yaml
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
| `cache.directory` | `.cache/eliotbot` | 本地快照与缓存位置 |
| `output.directory` | `reports` | 真实分析默认报告根目录 |
| `output.formats` | `[html, md]` | 默认报告格式；JSON 始终保存 |

配置字段必须符合样例结构，未知字段会报错。`search.max_subwave_depth` 允许 `0/1/2`；`1` 对已确认父腿做一层子模板规则认证，`2` 再向下认证一层。`--long-term` 会把它设为 1，显式 `--subwave-depth 0` 可覆盖。`demo` 有独立的默认输出目录和格式，需要通过其 `--output-dir`、`--format` 修改。

已暴露为命令参数的设置，其优先级为：显式命令参数 > `ELIOT_` 环境变量 > YAML 配置 > 内置默认值。例如只对一次调用改变候选数：

```bash
ELIOT_ANALYSIS__TOP_K=5 .venv/bin/eliot analyze 600519
```

需要自己的脚本读取结果时，使用 `--json`，诊断和错误会走 stderr：

```bash
mkdir -p reports
.venv/bin/eliot analyze 600519 --format json --json > reports/600519-result.json
```

批量分析目前通过逐只调用实现，没有独立的股票池命令。例如依次分析 3 只股票，失败时输出股票代码后继续：

```bash
for symbol in 600519 000001 300750; do
  if ! .venv/bin/eliot analyze "$symbol" --timeframe 1d; then
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
| `--open` | 导出 HTML 后尝试打开浏览器，仅 `analyze` 支持 |

```bash
.venv/bin/eliot --help
.venv/bin/eliot analyze --help
.venv/bin/eliot demo --help
.venv/bin/eliot data fetch --help
.venv/bin/eliot data inspect --help
.venv/bin/eliot replay --help
```

## 12. 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `eliot: command not found` | 使用 `.venv/bin/eliot`，或先 `source .venv/bin/activate` |
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
| 快照周期不匹配 | 使用快照原周期，或日线 → 周线；小时线需单独获取 |
| 1 分钟线每天少两个时隙 | 新浪现代沪深股票 profile 对 14:57、14:58 起始的无成交集合竞价时隙有明确处理；其余未知缺口仍会阻断分析 |
| PNG 导出失败 | 使用 `--renderer svg --format html,png,md`，或安装并配置 Plotly/Kaleido 的完整运行条件 |
| HTML 没有自动打开 | `demo` 不支持 `--open`；无桌面环境时手动下载并打开 HTML |

常用退出码：`0` 表示命令正常结束，`2` 表示输入或配置错误，`3` 表示数据不足/陈旧，`4` 表示来源或权限失败，`6` 表示报告导出失败。`UNCLEAR`、`SEARCH_LIMITED` 可以正常结束，因此自动化脚本还应读取 JSON 的 `status`，不能只看退出码。

当前交付范围和真实数据证据见[实现与验收状态](IMPLEMENTATION_STATUS.md)；算法与后续开发设计见[详细实现方案](IMPLEMENTATION_PLAN.md)。
