# `elliottwaver brief` A 首版验收记录

日期 2026-11-19。范围是 A 组“收盘摘要与自动选窗”的首版：单页摘要、自动周期、主选/备选、条件/目标、父级/子浪证据和数据警告。B–G 未纳入。

源码指纹 `39320541e668ec9fcfe413a342630a3bcb7e937d1ff2ebc8c707cc507fe03dde`，引擎 `ew-rules-v3/zigzag-v1/score-v2/subwave-v6/projection-v4/report-v4/search-v2/0.1.0`。

## 已实现

- 新增 `elliottwaver brief SYMBOL`，Typer 和 argparse fallback 同时注册。
- 新增 `Brief` 严格契约：窗口、卡片、问题、概率状态。
- 新增 `build_brief` 与 `AnalysisService.analyze_prepared`，避免摘要路径重复准备输入。
- 新增 `publish_brief`：`brief.json`、`daily.json`、`report.md`、`index.html`、`daily.html`、`manifest.json`。
- 终端摘要 `render_brief`。
- 自动周期：默认日线；快照为周线或请求跨度超过 5 年时自动周线；显式 `--timeframe 1d/1w` 可覆盖。
- 候选卡片：主选 + 最多两个同浪级、不同结构键的备选；显示父级、子浪、质量、覆盖、权重、条件、目标区和差异。
- 数据警告结构化：`ANALYSIS_WINDOW_TRUNCATED`、`EXCLUDED_STRUCTURE`、`NO_QUALIFIED_CANDIDATE` 等。
- 概率状态固定 `UNCALIBRATED`，不把候选权重写成成功概率。

## 验收

```text
185 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
```

A1 烟测（两只个股 + 两个指数，离线快照）：

| 标的 | 快照 | 状态 | 卡片 |
| --- | --- | --- | ---: |
| 600519.SH | `8378a32d...` | UNCLEAR | 2 |
| 000001.SZ | `274c1b50...` | OK | 3 |
| 000300.SH | `9d87a585...` | OK | 2 |
| 000001.SH | `195886c5...` | OK | 2 |

同码不同市场 `000001.SH` 与 `000001.SZ` 分别输出，未混淆。`600519.SH` 周线在无合格候选时返回空卡片和 `NO_QUALIFIED_CANDIDATE`，报告仍可打开。

## 产物示例

```text
reports/CN/600519.SH/brief/2026-03-31/<brief_digest>/
  brief.json
  daily.json
  report.md
  index.html
  daily.html
  manifest.json
```

`manifest.json` 记录 `brief_digest`、源快照、分析输入、结果摘要、引擎版本、配置摘要和逐文件 SHA-256。

## 复现

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/elliottwaver brief 600519.SH --offline \
  --snapshot 8378a32d2ce12a8419cb853ad4c6558aa4644f11aae7302c3319d14530d87f8d \
  --output-dir reports/brief-check --format html,md
```

## 边界

- 只做单个主周期；周线背景、换窗复核、历史索引、watchlist、概率模型和盲标未实现。
- 自动周期是简单规则，不是完整的“合适周期”判断。
- 指数分钟线未启用；北交所个股未接入。
- 价格概率未校准；短期走势只提供条件情景。
- 本次未提交，等待验收。
