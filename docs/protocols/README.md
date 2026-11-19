# 概率评估协议

本目录保存短期走势与候选事件的评估协议。仓库中的 JSON 固定了样本、快照 ID、决策网格、数据截止、分段和发布门槛。手册说明如何阅读评估、训练和支持矩阵结果。[离线评估与概率研究](../USER_GUIDE.md#48-离线评估与概率研究)

## `roadmap-repair-2026-heldout-smoke.json`

当前手册使用的端到端验证协议。茅台、平安银行、上证指数和沪深 300 使用四份日线快照。训练与校准使用 2023–2025 年数据，留出取 2026-04-01 至 2026-09-30，结果观察截止为 2026-09-30。每周采一个决策点，按交易会话计算 5／20 日结果。个股与指数分别训练。每类只有两个标的，低于发布门槛。

运行前，按 `samples[].snapshot_id` 逐个执行 `eliot data inspect ID`。默认缓存位置为 `.cache/eliotbot`，协议内的 `cache_root` 须指向快照所在的缓存目录。

```bash
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/roadmap-repair-2026-heldout-smoke.json \
  --output-dir reports/eval
.venv/bin/python scripts/train_forecasts.py \
  --protocol docs/protocols/roadmap-repair-2026-heldout-smoke.json \
  --decisions reports/eval --output-dir reports/models
```

新环境没有原始快照时，先复制协议，再分别取数。以下起点保留了 2023 年首批决策所需的历史，`--as-of` 固定数据观察日期。

```bash
mkdir -p reports
cp docs/protocols/roadmap-repair-2026-heldout-smoke.json reports/protocol-local.json
for symbol in 600519.SH 000001.SZ 000001.SH 000300.SH; do
  .venv/bin/eliot data fetch "$symbol" --timeframe 1d \
    --start 2022-01-01 --as-of 2026-09-30
done
```

按标的把每条命令输出的 `snapshot_id` 填入本地协议副本；如改过缓存路径，同步修改 `cache_root`。评估和训练都使用 `--protocol reports/protocol-local.json`。新下载的历史行情可能含供应商修订，应记录新快照和结果作为一次新评估。

## `e-expanded-2023-2025.json`

此为早期试跑协议，结果归档为探索记录。旧版明细格式需要用当前评估器重新生成才能训练。

- 样本：16 个日线标的（11 只沪深个股、5 个沪深指数）和 8 个周线标的（5 个原生周线、3 个由长历史日线派生）。
- 范围：2023-01-01 至 2025-12-31；周度决策网格 `LAST_SESSION_OF_WEEK`。
- 分割：训练 2023-01-01 至 2024-06-30；校准 2024-07-01 至 2025-03-31；留出 2025-04-01 至 2025-12-31。
- 事件：
  - 日线：`CLOSE_BAND_5D_V1`、`CLOSE_BAND_20D_V1`、`CONFIRM_FIRST_20D_V1`、`TARGET_FIRST_20D_V1`
  - 周线：`CLOSE_BAND_5W_V1`、`CLOSE_BAND_20W_V1`、`CONFIRM_FIRST_8W_V1`、`TARGET_FIRST_8W_V1`
- 发布门槛：`release` 字段，包含可靠性分档、最小分档样本/日期、可靠性容差、测试样本/日期/标的门槛、支持档覆盖率、2000 次配对块重抽样和固定 seed。

两份协议都依赖本地快照。要复现原有结果，请保留原快照；准备新样本时，使用本地协议副本记录更换后的快照 ID。

## 早期协议复跑

```bash
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/e-expanded-2023-2025.json \
  --output-dir reports/eval-expanded

.venv/bin/python scripts/train_forecasts.py \
  --protocol docs/protocols/e-expanded-2023-2025.json \
  --decisions reports/eval-expanded \
  --output-dir reports/models-expanded
```

评估输出 `decisions.jsonl`、`events.parquet`、`summary.json`、`manifest.json`。训练按每个独立品种与行情口径输出 `model.json`、`summary.json` 和 `manifest.json`。

## 样本边界

设计建议约 30 只分层沪深个股和 4 个代表指数。当前本地可用快照为 11 只个股和 5 个指数；协议如实记录该限制。若留出检验未达到门槛，结论只针对该样本，不能外推为全市场已校准概率。
