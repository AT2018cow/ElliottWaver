# 概率评估协议

本目录保存短期走势与候选事件的评估协议。仓库中的 JSON 固定了样本、快照 ID、决策网格、数据截止、分段和发布门槛。手册说明如何阅读评估、训练和支持矩阵结果。[离线评估与概率研究](../USER_GUIDE.md#48-离线评估与概率研究)

## `wave-engine-acceptance-v1*.json`

多浪级高级引擎的结构验收协议。CN 与美股各自有 `classic_v4` 和 `hierarchical_v1` 两个版本，样本和截止网格相同，`events` 为空，仅用于结构/覆盖/性能对照。高级配置示例见仓库根目录 `config.advanced.example.yaml`。[首版验收](../reviews/2026-11-20-wave-engine-acceptance.md)

## `wave-engine-variant-fixtures.json`

WE-3 亚型正式入口的冻结合成快照清单，覆盖平台三变体、阻挡三角形、截短第五浪、收缩/引导/终结楔形和 WXY 双重三浪。快照由 `scripts/make_variant_fixtures.py` 重新生成；正式入口验收使用仓库根目录 `config.variant.example.yaml`（额外含 `8.0` 父级尺度、`beam_width=1024`、`max_bridge_intervals=21`）。`leading_diagonal` 记录显式 `anchor`，使父推动和楔形同时进入搜索域。逐亚型结果见[独立验收清单](../reviews/2026-11-20-wave-engine-independent-acceptance.md)，回归测试为 `tests/test_variant_fixtures_end_to_end.py`；楔形内部 3-3-3-3-3/5-3-5-3-5 由 `tests/test_hierarchical_engine.py` 覆盖。

## `cn-probability-release-2026.json`

30 只沪深个股和 7 个指数的独立发布验证。分别训练个股和指数的日线事件模型，使用 2026 年留出期。发布门槛沿用前序协议。[范围、分段与复现](cn-probability-release-2026.md)

指数 20 日目标先达已完成开发期重新校准和参数冻结。后续在固定新数据上验证。[冻结候选与前向验证流程](cn-index-target-forward.md)

V3 目标几何与滚动校准使用[七指数开发协议](cn-index-target-v3-2023-2025.json)重放 2023–2025。四组对照与失效审计见[开发期验收](../reviews/2026-11-19-cn-index-target-accuracy.md)。该轮没有新增独立留出，状态为研究完成、待新留出，不替换上面的 V2 冻结候选。

改进准确度的研究范围、模块契约与对照实验见[指数 20 日目标先达准确度改进计划](cn-index-target-accuracy-work-plan.md)。

## `cn-index-target-v3-2023-2025.json`

7 个沪深京指数的开发期协议，使用 `sina-index` 无复权日线、`TARGET_FIRST_20D_V1`、每周最后一个交易日和 `feature_version=3`。网格为 2023-01-01 至 2025-12-31，结果观察截止 2026-11-16；分段为训练 2023–2024、校准 2025 上半年、验证 2025 下半年。该协议只服务 A–D 四组对照和 E1/E2 回归，不作为发布审批协议。

## `cn-index-target-history-2021-2022.json`

七指数历史压力测试。2021 年训练、2022 年上半年校准、下半年评分。沿用 V3 开发协议的日线快照和发布门槛开展回放研究。[结果与复现](../reviews/2026-11-20-cn-index-probability-recheck.md#2022-年历史压力测试)

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

早期扩大样本协议包含 11 只个股和 5 个指数。本轮发布验证使用另一份冻结名单。结论的适用范围以各协议的快照、日期和样本为准。

## 短期事件的历史与前向评分

先用 `evaluate_briefs.py` 生成开发评估，再用 `train_forecasts.py freeze` 拟合指定事件和候选位置。历史留出协议使用 `HISTORICAL_HOLDOUT`，以常规回放生成留出评估，运行 `score --candidate CANDIDATE.json --holdout HOLDOUT_DIR --output-dir SCORE_DIR`。显式 `--frozen-at` 表示模拟历史切分，实际参数冻结时间保存在 `frozen_at` 字段。历史评分报告保留指标和原因，发布状态为回顾性研究。

正式前向路径使用实际冻结后的新候选。参考 `prediction-outlook-holdout-*-v1.json` 准备未来网格、标的、事件和其他规则。先运行 `seal` 保存计划。在网格规定的决策日保留正常实时 `eliot brief` 生成的 `LIVE` 报告和发行快照。到期后仅填入已经取得的结果快照 ID，把协议状态改为 `SEALED_FORWARD_HOLDOUT`，再执行回放和评分。

```bash
.venv/bin/python scripts/train_forecasts.py seal \
  --candidate CANDIDATE.json --protocol PLAN.json --output SEAL.json
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol PLAN.json --live-reports LIVE_REPORTS_DIR --output-dir HOLDOUT_DIR
.venv/bin/python scripts/train_forecasts.py score \
  --candidate CANDIDATE.json --holdout HOLDOUT_DIR \
  --seal SEAL.json --live-reports LIVE_REPORTS_DIR --output-dir SCORE_DIR
```

将大写占位路径替换成实际文件或目录。实时报告目录每标的、周期和决策日应只有一份指定发行，可保留同一报告的多种导出格式。带 `--offline` 或 `--as-of` 的复算会生成冻结回放报告。正式评分核对封存计划、源码身份、真实发行简报及其快照，再应用现有概率门槛。
