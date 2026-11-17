# EliotBot

面向 A 股与美股的艾略特波浪分析程序，按“数据获取 → 多尺度拐点 → 候选数浪 → 规则校验 → 评分与点位 → 图表报告”组织能力。

当前版本为 **0.1.0 CLI MVP**：已经实现行情获取、冻结快照、因果拐点、普通推动浪/ABC 锯齿候选、评分、关键价位和报告。

支持 `1m / 5m / 15m / 30m / 60m / 1d / 1w`，`1h` 是 `60m` 的别名。候选权重表示相对评分，统计成功概率保持未校准状态。完整证据与范围见[实现及验收状态](docs/IMPLEMENTATION_STATUS.md)。

首次使用请阅读[中文使用手册](docs/USER_GUIDE.md)，包含从启动到查看报告的完整步骤、多周期命令、结果解读、离线快照、回放和常见问题。

## 安装

Python 3.11+；本次验证环境为 Linux / Python 3.12。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,cn,us]'
.venv/bin/eliot doctor
```

`requirements.lock` 保存本次通过验证的完整依赖版本，可先安装该文件，再使用 `pip install --no-deps -e .` 安装项目。锁定文件包含 A 股、美股 SDK 和开发依赖。

## 先看离线演示

无需密钥或网络，所有演示图表会明确标记为合成行情：

```bash
.venv/bin/eliot demo
.venv/bin/eliot demo --symbol 600519 --timeframe 1h
.venv/bin/eliot demo --timeframe 1w
.venv/bin/eliot demo --renderer svg --format html,png,md
```

默认输出 `analysis.json`、`report.md` 和可离线打开的 Plotly `chart.html`，终端会给出目录及快照 ID。HTML 中可以切换候选剧本、缩放和查看拐点确认时间。SVG 渲染器可以直接生成 PNG；Plotly 的 PNG 导出还需安装 `.[png]` 和相应浏览器运行环境。

## 分析真实行情

沪深 A 股无需 API key：

```bash
.venv/bin/eliot analyze 600519
.venv/bin/eliot analyze 000001 --timeframe 1w
.venv/bin/eliot analyze 300750 --timeframe 60m
.venv/bin/eliot analyze 600519 --timeframe 5m --output-dir reports/my-scan
```

美股先复制 `.env.example` 为 `.env`，在本地填写 `ALPACA_API_KEY`、`ALPACA_API_SECRET`，按账户选择 paper/live `ALPACA_API_ENDPOINT`：

```bash
.venv/bin/eliot analyze AAPL --feed sip
.venv/bin/eliot analyze MSFT --timeframe 1h --feed sip
.venv/bin/eliot analyze AAPL --feed iex --timeframe 15m
```

默认 SIP，权限失败不会换成 IEX。美股分钟/小时线采用 RTH 开盘锚定聚合；原生日线采用供应商日线口径，保守等到纽约日期结束并经过访问缓冲后再视为完整，和 RTH 收盘不是同一截止时刻。

A 股小时线不跨午休。周线从先复权的日线聚合，只分析完整周。新浪分钟历史长度随周期变化，不能保证任意历史日期都有分钟数据；当前 1 分钟样本约有 1,970 根，而不是完整 90 日。收盘集合竞价的已知无成交分钟时隙不补造 K 线，其他未知缺口会显示数据不足。

## 快照、离线复现和回放

```bash
.venv/bin/eliot data fetch 600519 --timeframe 1d

# 将下面的值替换为上一条命令输出的 snapshot_id
SNAPSHOT_ID='替换为完整snapshot_id'
.venv/bin/eliot data inspect "$SNAPSHOT_ID"
.venv/bin/eliot analyze 600519 --offline --snapshot "$SNAPSHOT_ID"
.venv/bin/eliot replay "$SNAPSHOT_ID" --start 2025-01-01 --step 5 --output reports/replay.jsonl
```

离线分析默认沿用快照周期；日线快照也可以通过 `--timeframe 1w` 生成周线分析。每份快照按内容寻址，Parquet 文件与元数据都校验 SHA256。回放使用同一引擎逐前缀计算，并写入带源快照 ID 和文件校验值的 manifest。它属于冻结数据回放，不能直接标成当时可取得数据的 PIT 验证。

配置使用当前 [config.example.yaml](config.example.yaml)，通过 `--config config.yaml` 加载；支持 `ELIOT_ANALYSIS__TOP_K=5` 等环境变量覆盖。凭证只放环境或 `.env`。`--json` 将结构化结果写 stdout，错误与诊断写 stderr。

`UNCLEAR` 是正常的分析结果；主数据不足/陈旧返回退出码 3，来源或权限错误返回 4，必需报告导出失败返回 6。

## 验证与当前边界

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

本次 81 项测试通过；真实新浪行情完成 3 只股票、9 组股票/周期组合的快照分析与报告验证，覆盖全部七种周期。美股适配器通过冻结 HTTP 契约测试，尚未使用真实账户密钥联调。

当前只枚举普通推动浪和 ABC 锯齿，不自动把重叠推动改名为楔形；内部子浪、父级位置、平台/三角形/楔形、北交所接入、概率校准和 Web 是后续阶段。子浪分形没有取得证据时明确显示 `UNAVAILABLE`，配置不能将其伪装为已开启。

- [原始系统设计](DESIGN.md)：产品目标、核心思路和交互设想。
- [中文使用手册](docs/USER_GUIDE.md)：安装、实际操作、结果解读、配置及排错。
- [详细实现方案](docs/IMPLEMENTATION_PLAN.md)：参考代码依据、设计修订、数据契约、算法细节、开发任务和验收标准。
- [实现及验收状态](docs/IMPLEMENTATION_STATUS.md)：当前完成范围、验收证据、已知边界和复现方式。
- [真实快照验证摘要](docs/validation_20261117.json)：输入/结果身份、覆盖区间、规则和前缀一致性。
