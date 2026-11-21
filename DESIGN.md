# EliotBot 系统架构与详细设计文档

本文保留原始产品设计。原设计中的剧本评分在当前实现中归一化为候选相对权重。日用版以可复核的波浪规则和分析报告为验收依据，事件概率使用独立的留出校准。[产品路线图](docs/ROADMAP.md) · [中文使用手册](docs/USER_GUIDE.md) · [实现及验收状态](docs/IMPLEMENTATION_STATUS.md)

> **项目定位**：基于艾略特波浪理论（Elliott Wave Theory）的自动化股票走势分析、关键点位推演与概率评估系统  
> **覆盖市场**：A 股（新浪财经数据源） & 美股（Alpaca 数据源）  
> **演进路线**：Phase 1 实现命令行（CLI）+ 交互式/静态图表输出；Phase 2 无缝扩展为 Web 交互应用

---

## 1. 设计理念与核心挑战解决思路

艾略特波浪理论在程序化落地时最大的难点是**“千人千浪”（多解性与主观性）**以及**“实时行情处于未完成浪中”**。本系统的核心设计原则不是强行给出唯一预测，而是构建一套客观的**概率推演引擎**：

1. **核心与展现解耦（CLI-First, Web-Ready）**：所有数据抓取、波浪计算、概率评分与点位推演均输出标准化的结构化数据模型（Pydantic Schema）。CLI 层负责终端排版与本地出图，未来 Web 层只需挂载 FastAPI 接口即可直接复用全部核心能力。
2. **自适应时间跨度（Anchor-Based Span）**：摒弃固定天数截取导致的“斩断浪起点”问题，通过大周期极值锚定自动截取完整波浪周期的 K 线。
3. **规则过滤 + 多维评分归一化**：
   - 用**三大铁律**做硬性剪枝（违反即淘汰，概率归零）；
   - 用**波浪指南**（斐波那契比例、MACD 动量背离、成交量特征、交替原则、通道吻合度、子浪分形）做软性打分，转化为各剧本的客观概率。
4. **可证伪的交易剧本输出**：每种候选走势均绑定明确的 **铁律失效位（Invalidation）**、**启动确认位（Confirmation）** 与 **斐波那契目标区（Target Zones）**。

---

## 2. 系统架构与目录规划

采用核心引擎库（`eliotbot`）与入口层（`cli` / `web`）分离的分层架构：

```text
EliotBot/
├── DESIGN.md                    # 系统设计文档
├── pyproject.toml               # 项目依赖与打包配置 (Python 3.10+)
├── config.example.yaml          # 配置文件模板 (API Key、算法超参数)
├── .env.example                 # 环境变量模板 (ALPACA_API_KEY 等)
│
├── eliotbot/                    # 【核心引擎包 - CLI 与 Web 共享】
│   ├── __init__.py
│   ├── config.py                # 配置加载与校验
│   ├── schemas/                 # 统一数据结构定义 (Pydantic Models)
│   │   ├── market.py            # K线 (OHLCV)、股票元信息、周期枚举
│   │   └── wave.py              # 拐点、浪型节点、候选剧本、关键位置、完整分析报告模型
│   │
│   ├── data/                    # 【数据获取与自适应跨度层】
│   │   ├── router.py            # 股票代码自动识别路由 (A股 vs 美股)
│   │   ├── base.py              # 数据源抽象基类 (DataProvider)
│   │   ├── sina_provider.py     # A股新浪财经 K线抓取 + 前复权因子计算
│   │   ├── alpaca_provider.py   # 美股 Alpaca Market Data v2 K线获取
│   │   ├── cache.py             # 本地轻量缓存 (SQLite / Parquet，避免重复请求限流)
│   │   └── span_selector.py     # 多周期联动与自适应时间跨度截取器
│   │
│   ├── engine/                  # 【波浪分析核心算法层】
│   │   ├── indicators.py        # 技术指标计算 (ATR, MACD, RSI, Volume MA)
│   │   ├── zigzag.py            # 基于 ATR 的多尺度自适应 ZigZag 拐点提取器
│   │   ├── rules.py             # 艾略特三大铁律与调整浪硬约束校验器
│   │   ├── templates.py         # 进行中/已完成浪型状态模板 (5浪推动、楔形、ABC调整、三角形)
│   │   ├── matcher.py           # 约束回溯搜索器 (枚举多尺度下合法的候选浪型计数)
│   │   ├── scorer.py            # 多维度特征打分与 Softmax 概率归一化引擎
│   │   └── projector.py         # 未来路径推演、斐波那契共振目标区与失效位计算器
│   │
│   ├── visualization/           # 【图表渲染与报告生成层】
│   │   ├── chart_builder.py     # Plotly 图表构建器 (同时支持导出 HTML/PNG 及返回 Web JSON)
│   │   └── report_formatter.py  # Markdown / 纯文本报告生成器
│   │
│   └── service.py               # 统一门面服务：`analyze_symbol(symbol, ...)` -> AnalysisResult
│
├── cli/                         # 【Phase 1：命令行交互入口】
│   ├── main.py                  # Typer CLI 入口 (参数解析、进度条展示)
│   └── console_render.py        # Rich 终端彩色面板与表格输出
│
├── web/                         # 【Phase 2：Web 服务预留目录】
│   ├── api/                     # FastAPI 路由 (调用 eliotbot.service)
│   └── frontend/                # 前端单页应用 (渲染 Plotly JSON / TradingView Lightweight Charts)
│
└── tests/                       # 单元测试与标准浪型合成数据回测
```

---

## 3. 核心数据模型设计 (`eliotbot/schemas/`)

为了确保 Phase 1 (CLI) 向 Phase 2 (Web) 平滑过渡，核心服务层输出严格类型的 JSON 友好结构：

```python
from datetime import datetime
from typing import Literal, List, Optional
from pydantic import BaseModel

class PivotPoint(BaseModel):
    index: int
    timestamp: datetime
    price: float
    kind: Literal["HIGH", "LOW"]
    confirmed: bool              # 最后一个拐点若仍随最新K线变动，则标记为未完全确认

class WaveLabel(BaseModel):
    label: str                   # 如 "(0)", "(1)", "(2)", "(3)", "(4)", "(5)" 或 "(A)", "(B)", "(C)"
    pivot: PivotPoint
    sub_wave_valid: Optional[bool] = None  # 次级别分形结构是否验证通过

class PriceZone(BaseModel):
    low: float
    high: float
    label: str                   # 如 "浪5第一目标区 (0.618x W1-3 & 1.0x W1 共振)"

class KeyLevels(BaseModel):
    invalidation_price: float    # 铁律失效位（破位则此剧本概率归零）
    invalidation_reason: str     # 如 "跌破浪1高点 196.30，违反浪4不与浪1重叠铁律"
    confirmation_price: Optional[float]    # 下一阶段确认突破位
    confirmation_reason: Optional[str]
    support_levels: List[float]
    resistance_levels: List[float]
    target_zones: List[PriceZone]# 按保守 -> 标准 -> 激进排序的目标区间

class ProjectedPoint(BaseModel):
    label: str                   # 推演未来节点标签，如 "(5)?" 或 "(C)?"
    estimated_bars: int          # 基于斐波那契时间周期预估的K线跨度
    target_price: float          # 基准推演价格（用于画虚线箭头）

class ScoreBreakdown(BaseModel):
    fibonacci_score: float       # 斐波那契比例吻合分 (0-40)
    momentum_volume_score: float # MACD背离与量价特征分 (0-25)
    guideline_channel_score: float # 交替原则与通道吻合分 (0-20)
    fractal_subwave_score: float # 次级别子浪结构一致性分 (0-15)
    total_score: float           # 总分 (0-100)
    highlights: List[str]        # 得分亮点说明（用于向用户解释原因）
    warnings: List[str]          # 扣分或风险提示

class WaveScenario(BaseModel):
    scenario_id: str             # 如 "S1", "S2"
    name: str                    # 如 "上升推动浪 - 第4浪回调末端，酝酿第5浪"
    direction_bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    pattern_type: Literal["IMPULSE", "LEADING_DIAGONAL", "ENDING_DIAGONAL", "ZIGZAG_ABC", "FLAT_ABC", "TRIANGLE_ABCDE"]
    current_stage: str           # 如 "处于浪(4)回调尾声 或 浪(5)初期"
    probability: float           # 归一化概率，如 0.542 (54.2%)
    completed_waves: List[WaveLabel]
    projected_path: List[ProjectedPoint]
    key_levels: KeyLevels
    score_detail: ScoreBreakdown

class AnalysisResult(BaseModel):
    symbol: str
    market: Literal["CN", "US"]
    timeframe: str               # 主分析周期，默认 "1d"
    anchor_date: datetime        # 自动截取的波浪锚点起始日期
    latest_price: float
    analyzed_at: datetime
    scenarios: List[WaveScenario]# 按 probability 降序排列的候选剧本 (Top 3~4)
```

---

## 4. 数据获取与自适应时间跨度设计 (`eliotbot/data/`)

### 4.1 股票代码自动识别路由 (`router.py`)
用户输入单一代码参数即可自动分流：
- **A 股识别规则**：匹配 `^(sh|sz|bj)?\d{6}$` 或 `^\d{6}\.(SH|SZ|BJ)$`
  - 若纯 6 位数字自动补全前缀：`6` 开头 -> `sh`（沪市/科创板），`0`/`3` 开头 -> `sz`（深市/创业板），`4`/`8`/`92` 开头 -> `bj`（北交所）。
- **美股识别规则**：匹配 `^[A-Za-z]+([.\-][A-Za-z]+)?$`（如 `AAPL`, `NVDA`, `BRK.B`），自动转为大写路由至 Alpaca。

### 4.2 A 股新浪数据源 (`sina_provider.py`)
1. **行情接口**：调用新浪财经 `CN_MarketData.getKLineData` 接口，支持 `scale = 5, 15, 30, 60, 240(日线), 1200(周线)`，单次最多拉取 `datalen=1023` 根 K 线。
2. **前复权处理（关键）**：
   - 股票分红送转产生的除权缺口会严重破坏波浪比例甚至误触发“浪4与浪1重叠”。
   - 日线/周线同步拉取新浪财经**复权因子接口**，按最新复权因子将历史 `open, high, low, close` 统一折算为**前复权（QFQ）**价格；分钟级 K 线结合最新日线复权比例对齐。
3. **稳定性保障**：内置请求头模拟、指数退避重试与本地缓存（当日已收盘周期直接读缓存）。

### 4.3 美股 Alpaca 数据源 (`alpaca_provider.py`)
1. **SDK 与认证**：使用官方 `alpaca-py` (`StockHistoricalDataClient`)，从环境变量或配置文件读取 `ALPACA_API_KEY` 与 `ALPACA_SECRET_KEY`。
2. **参数配置**：
   - 使用 `StockBarsRequest`，设置 `adjustment=Adjustment.ALL`（自动处理拆股与分红前复权）。
   - 支持免费账户默认 `DataFeed.IEX` 与付费账户 `DataFeed.SIP` 可配置切换（历史日线在免费层同样可完整获取）。

### 4.4 自适应时间跨度选择器 (`span_selector.py`)
当用户仅指定股票代码（未强制指定起止时间）时，执行**“三级周期锚定算法”**：

1. **Step 1 - 周线寻锚（Macro Anchor）**：
   - 拉取最近 3~5 年（约 200~250 根）周 K 线。
   - 计算较大尺度的 ZigZag（$k = 4.0 \times \text{ATR}_{14}$），定位距离当前最近的**全局显著极值点**（重要历史大底或历史大顶），且满足距今至少包含 60 根日 K 线（保证浪型有足够展开空间），至多不超过 600 根日 K 线（避免跨越多轮牛熊导致层级混乱）。
   - 若最近极值距今太近（如刚创历史新高/新低），则向前追溯上一个反向重要极值点作为完整趋势起点 `(0)`。
2. **Step 2 - 日线主分析（Primary Analysis）**：
   - 从锚点日期（Anchor Date）向前多取 30 根 K 线用于指标预热（MACD/ATR），截取至最新交易日作为主分析序列。
3. **Step 3 - 60 分钟线子浪验证（Sub-wave Refinement）**：
   - 拉取最近 60~90 个交易日的 60 分钟 K 线，仅用于校验日线级别“最近两浪”内部的子浪结构（5 子浪推动 vs 3 子浪调整），为概率评分提供分形依据。

---

## 5. 波浪分析核心引擎设计 (`eliotbot/engine/`)

### 5.1 多尺度 ATR 自适应拐点提取 (`zigzag.py`)
不同股票波动率差异巨大（如四大行日均波动 1%，妖股/科技股日均波动 5%+），固定百分比阈值无法通用。

- **动态反转阈值公式**：
  $$\Delta P_{\text{threshold}}(t) = k \times \text{ATR}_{14}(t)$$
- **多尺度并行扫描**：因为程序事先不知道当前处于哪个波浪层级，引擎会同时在 **3 个灵敏度尺度** 上提取拐点序列并分别搜索浪型，最后汇总去重评比：
  - **Major 尺度**（大波段）：$k = 3.8$
  - **Intermediate 尺度**（标准中波段）：$k = 2.4$
  - **Minor 尺度**（精细波段）：$k = 1.4$
- **边界与严格交替处理**：
  - 保证输出的拐点严格满足 `HIGH -> LOW -> HIGH -> LOW` 交替；
  - 将最新一根 K 线的收盘/极值作为**未确认动态末端点（Developing Tip）**纳入分析，以判断当前正在运行中的这一笔所处位置。

### 5.2 波浪状态模板与回溯匹配器 (`templates.py` & `matcher.py`)
实盘分析中，行情往往**正处于某一浪的运行途中**，而非恰好停在完整的第 5 浪或 C 浪终点。因此系统定义两大方向（上涨趋势 / 下跌趋势）下的**进行中浪态模板**：

| 形态大类 | 支持匹配的当前所处阶段（Developing Stages） | 核心结构要求 |
| :--- | :--- | :--- |
| **5浪推动 (Impulse)** | - **浪 3 主升/主跌途中** `(0)-(1)-(2)->[3进行中]`<br>- **浪 4 调整途中/末端** `(0)-(1)-(2)-(3)->[4进行中]`<br>- **浪 5 冲顶/赶底途中** `(0)-(1)-(2)-(3)-(4)->[5进行中]`<br>- **完整 5 浪刚结束，转入调整初期** `(0)..(5)完成` | 严格满足三大铁律 |
| **楔形 (Diagonal)** | - **引导楔形 (浪1/浪A)** 或 **终结楔形 (浪5/浪C)** | 允许浪 4 与浪 1 重叠，但上下轨连线必须呈现收敛（或扩散）楔形，且浪 3 不最短 |
| **锯齿形调整 (Zigzag A-B-C)** | - **B 浪反弹/反抽途中** `(0)-(A)->[B进行中]`<br>- **C 浪杀跌/上攻途中** `(0)-(A)-(B)->[C进行中]`<br>- **完整 ABC 调整末端（酝酿新一轮推动）** | B 浪回撤 < A 浪的 100%（理想 38.2%~78.6%），C 浪具备推动特征 |
| **平台形调整 (Flat A-B-C)** | - **规则平台 / 顺势扩散平台 (Expanded Flat)** | B 浪回撤达 A 浪的 90% ~ 138.2%，C 浪目标达 A 浪终点的 100% ~ 161.8% |
| **收敛三角形 (Triangle A-B-C-D-E)** | - **处于 D 浪或 E 浪末端，即将突破** | 5 段重叠结构，高点依次降低且低点依次抬高（常出现于浪 4 或 B 浪） |

**回溯搜索算法（CSP Backtracking）**：
- 允许对局部微小次级噪音拐点进行“合并跳跃（Pivot Merging）”——即某一大浪内部若包含次级折返（如 3 个小拐点构成一笔大浪），搜索器在满足极值单调性的前提下允许跨越中间次级拐点，从而克服单一 ZigZag 阈值过度切分的问题。

### 5.3 硬性约束校验器 (`rules.py`)
对任意候选拐点映射 $(P_0, P_1, P_2, P_3, P_4, P_5)$，执行一票否决检查：
1. **铁律 1（浪 2 底线）**：上涨推动中，$P_2 > P_0$（下跌推动中，$P_2 < P_0$）。
2. **铁律 2（浪 3 非最短）**。第 $i$ 浪以起点为基准计算百分比长度 $R_i = |P_i - P_{i-1}| / P_{i-1}$，要求 $R_3 \ge \min(R_1, R_5)$。浪 5 进行中且 $R_3 < R_1$ 时，失效边界为 $P_4 P_3 / P_2$。上涨影线高于该价或下跌影线低于该价时失效，恰好到达边界时仍可成立。
3. **铁律 3（浪 4 不重叠）**：普通推动浪中，上涨时 $P_4 > P_1$（下跌时 $P_4 < P_1$）。若发生重叠，仅当包络线斜率收敛且子浪呈 `3-3-3-3-3` 特征时才允许降级归入“楔形（Diagonal）”模板，否则直接淘汰。

完成的三角形在 E 终点必须形成对前一浪的净回撤。以 A 浪方向为正向时，要求 E 终点越过 A 起点。B 浪可以穿过 A 起点。

### 5.4 概率打分模型 (`scorer.py`)

每个通过硬规则校验的候选浪型，按 **100 分制** 计算综合质量分 $S$：

#### (1) 斐波那契比例吻合度（满分 40 分）
对于每个已完成浪段の実测比率 $r$，对照标准斐波那契比率集合 $\{r_k^*\}$，采用高斯核函数打分：
$$f(r) = \max_{k} w_k \cdot \exp\left(-\frac{(r - r_k^*)^2}{2\sigma^2}\right)$$

| 浪段关系 | 高分核心比率 $r_k^*$ (权重 1.0) | 次优可接受比率 (权重 0.7) | 容差标准差 $\sigma$ |
| :--- | :--- | :--- | :--- |
| **浪 2 回撤浪 1** | `0.618`, `0.500` | `0.786`, `0.382` | `0.06` |
| **浪 3 相对浪 1 扩展** | `1.618`, `2.618` | `1.382`, `2.000`, `4.236` | `0.15` |
| **浪 4 回撤浪 3** | `0.382` | `0.236`, `0.500` | `0.05` |
| **浪 5 相对浪 1 (或 0.618×浪1~3)** | `1.000` (等长), `0.618` | `1.618` (五浪延长) | `0.10` |
| **锯齿 B 浪回撤 A 浪** | `0.618`, `0.500` | `0.382`, `0.786` | `0.06` |
| **C 浪相对 A 浪扩展** | `1.000`, `1.618` | `0.618`, `1.272` | `0.10` |

#### (2) 动量（MACD）与成交量特征（满分 25 分）
- **浪 3 极值特征（+10 分）**：浪 3 期间的 `MACD DIF` 或 `MACD Histogram` 绝对值创整个 5 浪序列最高，且日均成交量高于浪 1。
- **浪 5 背离特征（+10 分）**：当价格在浪 5 创出超越浪 3 的极值时，`MACD` 峰值低于浪 3 峰值（经典顶/底背离）。
- **调整浪缩量特征（+5 分）**：浪 2、浪 4 或 B 浪期间的平均成交量较前一段推动浪萎缩 15% 以上。

#### (3) 波浪指南：交替原则与平行通道（满分 20 分）
- **浪 2 与浪 4 交替原则（+10 分）**：比较浪 2 与浪 4 的 `回撤深度比` 与 `持续时间比`。若浪 2 深而急（如回撤 >50% 且耗时短），而浪 4 浅而缓（如回撤 <38.2% 且横向耗时更长），则加高分；反之亦然。
- **艾略特趋势通道吻合度（+10 分）**：连接 `P1-P3` 作基准趋势线，过 `P2` 作平行通道线，衡量 `P4` 落点与该平行下轨的距离误差。

#### (4) 次级别子浪分形一致性（满分 15 分）
- 调用次一级灵敏度（或 60m K线）的拐点计数：若推动浪内部拐点数倾向于奇数笔（5、9 笔），调整浪内部拐点数倾向于 3 笔，则获得分形加分。

#### (5) 候选剧本去重与概率归一化
1. **结构去重**：不同 ZigZag 尺度若搜出同一方向、同一关键锚点且当前所处浪位相同的方案，保留得分最高者。
2. **筛选 Top-K**：保留得分 $\ge 40$ 分的前 $K$ 个剧本（默认 $K=3$），另外保留 `1 - sum` 或添加一个兜底的 `Unclear / Complex Consolidation` 提示（当所有候选最高分均偏低，如 $<45$ 分时，诚实告知用户当前浪型混沌，降低强行预测误导）。
3. **温度缩放 Softmax 计算概率**：
   $$P_i = \frac{\exp(S_i / \tau)}{\sum_{j=1}^{K} \exp(S_j / \tau)} \quad (\text{默认温度系数 } \tau = 12.0)$$

### 5.5 关键位置推演引擎 (`projector.py`)

针对排名前列的每个 `WaveScenario`，自动推演未来关键价格：

| 当前所处阶段示例 | 铁律失效位 (Invalidation) | 启动确认位 (Confirmation) | 斐波那契目标共振区 (Target Zones) |
| :--- | :--- | :--- | :--- |
| **上涨推动：浪(2)回调中** | $P_0$（浪1起点） | 突破 $P_1$（浪1高点）或突破浪2下降趋势线 | - 目标1: $P_2 + 1.0 \times W_1$<br>- 目标2 (主目标): $P_2 + 1.618 \times W_1$<br>- 目标3: $P_2 + 2.618 \times W_1$ |
| **上涨推动：浪(4)回调中** | $P_1$（浪1高点，不可重叠） | 突破浪4调整高点 / 突破 $P_3$（浪3高点） | 计算两套测算的**价格聚类共振带**：<br>① $P_4 + \{0.618, 1.0\} \times W_1$<br>② $P_4 + \{0.382, 0.618\} \times (P_3 - P_0)$<br>③ 艾略特通道上轨延伸位 |
| **上涨推动：浪(5)冲顶末期** | 浪5延伸超长导致浪3变最短的边界（若有），或跌破 $P_4$ 确认见顶 | 跌破浪4低点 $P_4$ 确认转入大级别 ABC 调整 | - 上方潜在见顶阻力区（同上）<br>- 见顶后首轮回撤目标：整个5浪涨幅的 `0.382` ~ `0.618`（通常对应 $P_4$ 附近） |
| **下跌调整：B浪反弹中** | 突破 $P_0$（A浪起点，若为锯齿形调整） | 跌破B浪反弹支撑趋势线 / 跌破 $P_A$（A浪低点） | - B浪反弹阻力区：$P_A + \{0.5, 0.618\} \times |W_A|$<br>- C浪下跌目标1：$P_B - 1.0 \times |W_A|$<br>- C浪下跌目标2：$P_B - 1.618 \times |W_A|$ |

---

## 6. 图表输出与 CLI 交互设计

### 6.1 可视化图表设计 (`visualization/chart_builder.py`)
采用 **Plotly** 构建三联图（高度占比 `65% : 15% : 20%`），既可生成**可缩放悬停查看详情的 `.html` 文件**，也可通过 `kaleido` 渲染输出**高清 `.png` 静态图片**：

1. **主图（Row 1：前复权 K 线 + 多剧本波浪叠加 + 关键价格带）**：
   - **K 线层**：A 股可配置“红涨绿跌”，美股可配置“绿涨红跌”（或统一配色）。
   - **首选剧本（Scenario 1，最高概率）**：
     - 已完成浪段：**粗实线（亮蓝色/金色）** 连接各拐点，拐点上方/下方带圆角标签框标注 `(0) (1) (2) (3) (4)`；
     - 未来推演路径：**粗虚线 + 箭头** 指向推演目标位及预估时间窗口（右侧预留 20~30 根空白 K 线宽度用于画未来推演区）。
   - **次选剧本（Scenario 2，次高概率）**：
     - 用**半透明紫/橙色点划线**绘制差异浪标（如 `[A] [B] [C]`）及备选未来推演路径（在 HTML 交互图中可通过点击右侧图例一键隐藏/切换单个剧本，避免视觉杂乱）。
   - **关键点位标注层**：
     - **绿色半透明矩形带（Target Zone）**：横跨未来推演区，右侧标注目标区间价格与概率依据；
     - **红色水平警戒线（Invalidation Level）**：标注 `❌ 失效位: xxx.xx`；
     - **蓝色点线（Confirmation Level）**：标注 `⚡ 确认突破位: xxx.xx`。
2. **副图 1（Row 2：成交量 Volume）**：叠加 20 日均量线，直观验证推动放量与调整缩量。
3. **副图 2（Row 3：MACD 12,26,9）**：当检测到浪 3 与浪 5 存在顶/底背离时，自动在主图价格极值与副图 DIF 极值之间画出**背离提示斜线**。

### 6.2 CLI 命令行交互设计 (`cli/`)

基于 `Typer` + `Rich` 实现简洁易用的命令行接口：

```bash
# 1. 最简用法：自动识别市场（A股/美股）、自动选择锚点跨度、输出终端报告 + 图表
eliot analyze 600519
eliot analyze AAPL

# 2. 进阶可选参数
eliot analyze sz300750 \
  --timeframe 1d \              # 可选强制指定主周期: 60m, 1d, 1w (默认自适应 1d)
  --top-k 3 \                   # 输出前 K 种高概率走势 (默认 3)
  --output-dir ./reports \      # 图表与 Markdown 报告保存目录
  --format html,png,md \        # 输出格式组合
  --open                        # 分析完成后自动在系统浏览器打开交互式波浪图
```

**终端输出效果（Rich 渲染）**：
- 顶部概要卡片：股票名称/代码、市场、当前价格、自适应锚点起点日期、浪型清晰度评级（高/中/混沌）。
- 走势剧本对比表：一目了然展示 Top 3 剧本的 **方向、当前阶段、概率进度条、失效止损位、确认位、第一/第二目标位**。
- 各剧本详细推演面板：展开评分细项亮点（如斐波那契实测比例 vs 理想比例对比表）与操作参考逻辑。

---

## 7. Phase 2：Web 版本无缝演进方案

由于 Phase 1 已经将核心逻辑完全封装在 `eliotbot.service.analyze_symbol()` 与 `Plotly Figure JSON` 中，后续升级 Web 版无需重写任何算法代码：

1. **后端（`web/api/`）**：
   - 使用 **FastAPI** 暴露两个核心接口：
     - `GET /api/v1/analyze/{symbol}?timeframe=1d`：直接返回 `AnalysisResult` JSON + Plotly 图表配置 JSON。
     - `GET /api/v1/watchlist`：批量扫描自选股池，筛选出“高清晰度（最高剧本概率 > 60%）且处于理想盈亏比位置（如浪2末端、浪4末端、C浪末端）”的标的。
2. **前端（`web/frontend/`）**：
   - 搜索框输入 A 股或美股代码；
   - 左侧/上方使用 `react-plotly.js` 或 `TradingView Lightweight Charts` 渲染 K 线与波浪推演图，支持**点击右侧剧本卡片动态切换图上显示的浪型与关键价格带**；
   - 右侧展示各走势剧本的概率仪表盘、关键价位距离当前价的涨跌幅百分比（如 `距失效位 -3.2% | 距第一目标 +11.5% | 潜在盈亏比 3.6 : 1`）。

---

## 8. 技术栈选型清单

| 模块 | 选型库 | 说明 |
| :--- | :--- | :--- |
| **运行环境** | Python 3.10+ | 类型注解友好，生态成熟 |
| **数据校验与模型** | `pydantic >= 2.0` | 核心 Schema 定义，无缝衔接未来 FastAPI |
| **数值与时序处理** | `pandas`, `numpy`, `scipy` | K线处理、向量化指标计算、极值辅助分析 |
| **A股数据获取** | `httpx` / `requests` | 对接新浪财经 K线与复权因子接口 |
| **美股数据获取** | `alpaca-py` | Alpaca 官方 Market Data v2 SDK |
| **绘图引擎** | `plotly`, `kaleido` | 交互式 HTML 波浪图构建与高清静态 PNG 导出 |
| **CLI 与终端美化** | `typer`, `rich` | 现代化命令行参数解析与彩色结构化终端面板 |
| **配置管理** | `pydantic-settings`, `pyyaml` | 支持 `.env` 与 `config.yaml` 混合管理密钥与参数 |
| **测试框架** | `pytest` | 标准推动浪/调整浪合成序列单元测试与实盘样本回归测试 |
| **Phase 2 Web 预留** | `fastapi`, `uvicorn`, `React/Vue` | 后续 Web 化前后端技术栈 |

---

## 9. 分阶段开发实施计划（Milestones）

### Phase 1：CLI 完整版（当前优先实现）
- **Milestone 1.1 - 项目骨架与数据通道**：
  - 初始化 `pyproject.toml`、配置管理与核心 Pydantic Schema；
  - 实现股票代码路由器、新浪 A 股前复权 K 线抓取器、Alpaca 美股抓取器与本地缓存；
  - 实现“周线寻锚 -> 日线截取 -> 60m 子浪辅助”的自适应时间跨度选择器。
- **Milestone 1.2 - 拐点提取与波浪规则搜索器**：
  - 实现 ATR、MACD、成交量均线等基础指标；
  - 实现多尺度 ATR ZigZag 拐点提取（含未完成末端动态点处理）；
  - 实现三大铁律校验器与回溯波浪匹配器（覆盖 5 浪推动、楔形、ABC 锯齿/平台调整、收敛三角形的进行中与已完成状态）。
- **Milestone 1.3 - 概率评分与关键位置推演**：
  - 实现斐波那契高斯打分、MACD 背离检测、交替与通道评分、子浪分形加分及 Softmax 概率校准；
  - 实现针对不同浪型阶段的失效位、确认位与斐波那契共振目标区推演器；
  - 编写标准波浪形态合成数据的单元测试。
- **Milestone 1.4 - 图表渲染与 CLI 封装**：
  - 实现 Plotly 多剧本波浪推演图（K线 + 主/次浪标 + 虚线预测箭头 + 目标区间带 + 失效线 + MACD背离连线）；
  - 实现 `eliot analyze <symbol>` CLI 命令、Rich 终端报告及 Markdown 报告导出。

### Phase 2：Web 交互版（后续迭代）
- **Milestone 2.1**：搭建 FastAPI 服务端，封装分析接口与异步缓存任务。
- **Milestone 2.2**：开发 Web 前端界面，支持剧本交互式切换高亮、自选股波浪形态批量扫描雷达。
