# 短期预测增强 P-08 真实样本全流程

对应[短期预测增强实施设计](../PREDICTION_ENHANCEMENT_PLAN.md)的 PE-6 P-08。使用现有冻结快照，不新增行情请求。

## 样本

| 市场 | 标的 | 类型 | 快照缓存 |
| --- | --- | --- | --- |
| CN | `600519.SH` | 个股 | `.cache/eliotbot` |
| CN | `000016.SH` | 指数 | `.cache/eliotbot` |
| US | `AAPL` | 个股 | `.cache/us-acceptance` |
| US | `SPY` | ETF（按 EQUITY 路由） | `.cache/us-acceptance` |

## brief 一致性

每个标的分别运行 JSON、Markdown、HTML 和终端输出：

```bash
.venv/bin/eliot brief <symbol> --offline --snapshot <id> \
  --config <cache-config> --no-weekly --no-window-check --format json --json
.venv/bin/eliot brief <symbol> --offline --snapshot <id> \
  --config <cache-config> --no-weekly --no-window-check --format md --output-dir <reports>
.venv/bin/eliot brief <symbol> --offline --snapshot <id> \
  --config <cache-config> --no-weekly --no-window-check --format html --output-dir <reports>
```

| 标的 | 状态 | outlook | 期限 | 观察期 | 卡片 | 终端 | Markdown | HTML |
| --- | --- | --- | --- | ---: | ---: | --- | --- | --- |
| `600519.SH` | OK | AVAILABLE | 5 SESSIONS | 5 | 2 | 有短期观察 | 有 | 有 |
| `000016.SH` | OK | AVAILABLE | 5 SESSIONS | 5 | 2 | 有短期观察 | 有 | 有 |
| `AAPL` | OK | AVAILABLE | 5 SESSIONS | 5 | 2 | 有短期观察 | 有 | 有 |
| `SPY` | UNCLEAR | AVAILABLE | 5 SESSIONS | 5 | 3 | 有短期观察 | 有 | 有 |

JSON、Markdown、HTML 和终端都包含同一个 `outlook` 的期限、当前腿、条件和事件状态；`SPY` 的 `UNCLEAR` 保留候选分歧，不抑制推演。

补充覆盖两个引擎和两个周期：

| 标的 | 配置 | 周期 | 状态 | outlook | 期限 |
| --- | --- | --- | --- | --- | --- |
| `600519.SH` | `config.advanced.example.yaml` | 1d | OK | AVAILABLE | 5 SESSIONS |
| `AAPL` | US advanced（缓存 `.cache/us-acceptance`） | 1d | OK | AVAILABLE | 5 SESSIONS |
| `600519.SH` | `config.advanced.example.yaml` | 1w | OK | AVAILABLE | 2 WEEKS |

## watchlist

```bash
.venv/bin/eliot watchlist watch-cn.txt --as-of 2026-11-18 --config <cn-config> \
  --no-weekly --no-window-check --json
.venv/bin/eliot watchlist watch-us.txt --as-of 2026-11-18 --config <us-config> \
  --no-weekly --no-window-check --json
```

结果：`600519.SH=OK`、`000016.SH=OK`、`AAPL=OK`、`SPY=UNCLEAR`，四个标的均生成 `brief_digest`。

## history

```bash
.venv/bin/eliot history 600519.SH --config <cn-config> --json
.venv/bin/eliot history AAPL --config <us-config> --json
```

两个市场均返回 `2026-11-18` 的 `OK` 记录；CN 历史中另有早前运行留下的 `UNREADABLE` 条目，不影响本次新报告。

## settle

```bash
.venv/bin/eliot settle reports/CN --cache-root .cache/eliotbot --json
.venv/bin/eliot settle reports/US --cache-root .cache/us-acceptance --json
```

| 市场 | 行数 | 事件 |
| --- | ---: | --- |
| CN | 20 | `CLOSE_BAND_5D_V1`、`CLOSE_BAND_20D_V1`、`CONFIRM_FIRST_20D_V1`、`TARGET_FIRST_20D_V1`、`ADVANCE_FIRST_5D_V1`、`OUTLOOK_TARGET_FIRST_5D_V1` |
| US | 24 | 同上 |

新事件使用 `evaluate_outlook_event`，与 `brief.outlook.cases` 的同一 `scenario_id` 和事件状态对应。

## support-matrix

```bash
.venv/bin/eliot support-matrix --json
.venv/bin/eliot support-matrix --model-summary pe5-score/summary.json --json
```

- 无模型汇总：26 行，覆盖 1d/1w 的收盘、确认、目标、推进和新 outlook 事件。
- 带 PE-5 冻结摘要：`ADVANCE_FIRST_5D_V1` 行显示 `outlook_version=1.0`、`outlook_slot=PRIMARY`、`INSUFFICIENT_EVIDENCE`，原因是训练分裂样本不足，不是作用域丢失。

## 结论

P-08 的冻结快照全流程已通过：单标的与 watchlist 结论一致，历史记录可读，核销包含新事件，support-matrix 显示新作用域，JSON/终端/Markdown/HTML 的推演内容一致。

正式前向 holdout 仍需冻结时刻之后的成熟行情；当前没有可用未来快照，`score` 入口已就绪但结果保持 `INSUFFICIENT_EVIDENCE`。
