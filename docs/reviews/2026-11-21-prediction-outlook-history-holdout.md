# 短期预测增强历史 holdout 结果

按用户要求改用历史数据完成 PE-4/PE-5 的独立留出，不再等待未来行情。冻结时刻固定在开发段结束，holdout 使用冻结时刻之后的既有冻结快照。

## 队列

| 协议 | 市场 | 开发窗口 | holdout 窗口 |
| --- | --- | --- | --- |
| `prediction-outlook-history-dev-cn-v1.json` | CN（7 标的） | 2024-01-01–2026-03-31 | — |
| `prediction-outlook-history-holdout-cn-v1.json` | CN | — | 2026-04-01–2026-11-18 |
| `prediction-outlook-history-dev-us-v1.json` | US（AAPL、SPY） | 2024-01-01–2026-03-31 | — |
| `prediction-outlook-history-holdout-us-v1.json` | US | — | 2026-04-01–2026-11-18 |

冻结时间：`2026-03-31T23:59:59+00:00`。候选：

| 候选 | 事件 | 市场 | candidate_id | 开发样本（train/cal/test） |
| --- | --- | --- | --- | --- |
| `history-candidate-cn-target.json` | `OUTLOOK_TARGET_FIRST_5D_V1` | CN | `85ba59e63c467d38` | 215 / 204 / 30 |
| `history-candidate-us-target.json` | `OUTLOOK_TARGET_FIRST_5D_V1` | US | `14ee1ef069fec438` | 188 / 184 / 48 |

`ADVANCE_FIRST_5D_V1` 初始开发测试样本只有 9/12 行，未达到拟合门槛；改用更长开发段后，CN 测试 64 行、US 测试 61 行，仍保持 `outlook_slot=PRIMARY` 和特征版本 4。

## ADVANCE 与 2 周收盘事件

| 事件 | 市场 | 候选 | 开发测试 | holdout 测试 | Brier | 状态 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `ADVANCE_FIRST_5D_V1` | CN | `95ab391d6248928f` | 64 | 36 | 0.5295 | `INSUFFICIENT_EVIDENCE` |
| `ADVANCE_FIRST_5D_V1` | US | `af4084d7ddbb4539` | 61 | 20 | 0.3433 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | CN | `a5597d896c8de5ea` | 124 | 124 | 0.8110 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | US | `6451d56c490776ae` | 126 | 62 | 0.6081 | `INSUFFICIENT_EVIDENCE` |

共同原因：

- ADVANCE：holdout 行数/标的不足，频率、动量和价格基线的配对区间下限未大于 0，支持档覆盖 0。
- 2 周收盘：标的数不足（CN 4/5，US 2/5），三类基线未改善，支持档覆盖 0。

`CLOSE_BAND_2W_V1` 使用 1w 协议和 `outlook_slot=SYMBOL`；ADVANCE 使用 1d 协议和 `outlook_slot=PRIMARY`。四个候选都已实际 freeze/score，未出现协议或作用域错误。

## 历史 holdout 评分

| 市场 | holdout 行数 | Brier | log loss | 相对频率 Brier 改善 | 相对动量 Brier 改善 | 相对价格 Brier 改善 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN | 214 | 0.1966 | 0.5764 | +0.0655（CI −0.0042–0.1337） | +0.1381（CI 0.0411–0.2412） | −0.0416（CI −0.0766–−0.0104） |
| US | 88 | 0.0824 | 0.2818 | +0.1225（CI 0.0576–0.1907） | +0.1353（CI 0.0886–0.1782） | −0.0146（CI −0.0432–0.0087） |

两个市场都未达到发布门槛：

- CN：`test symbols 4 < 5`；频率和价格基线的配对区间下限未大于 0；支持档覆盖 0。
- US：`test symbols 2 < 5`；价格基线未改善；支持档覆盖 0。

结论：历史 holdout 已完成，且给出可复核的负结果。当前没有获批模型，`support-matrix` 仍为 `INSUFFICIENT_EVIDENCE`；这不是接口缺失。

## 独立验收后的留出复算

独立验收发现旧评分把指数、synthetic feed、备选位置和未定义状态混入样本。当前工作树重新生成 CN/US 日线合并 holdout 与周线 holdout，再用同一批冻结候选评分。复算只改变留出窗口、标签和样本筛选；候选参数仍来自修复前的开发训练，因此这些分数只作回顾性研究，不用于批准。

| 事件 | 市场 | 新 holdout 行数 | Brier | 频率改善 | 动量改善 | 价格改善 | 状态 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `OUTLOOK_TARGET_FIRST_5D_V1` | CN | 46 | 0.1727 | +0.0703 | +0.1313 | −0.0194 | `INSUFFICIENT_EVIDENCE` |
| `OUTLOOK_TARGET_FIRST_5D_V1` | US | 24 | 0.0828 | +0.1898 | +0.1759 | +0.0092 | `INSUFFICIENT_EVIDENCE` |
| `ADVANCE_FIRST_5D_V1` | CN | 5 | 0.2089 | −0.2060 | −0.2059 | −0.2071 | `INSUFFICIENT_EVIDENCE` |
| `ADVANCE_FIRST_5D_V1` | US | 5 | 0.1989 | +0.2132 | +0.1091 | +0.0278 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | CN | 62 | 0.7310 | −0.1398 | −0.1699 | −0.1510 | `INSUFFICIENT_EVIDENCE` |
| `CLOSE_BAND_2W_V1` | US | 57 | 0.6094 | −0.1902 | −0.2024 | −0.1853 | `INSUFFICIENT_EVIDENCE` |

- 评分范围现在只保留冻结候选的 `market/asset_type/feed/adjustment/timeframe/symbols`，新事件还要求匹配 `outlook_slot`。CN 目标从 214 行降到 46 行，US 目标从 88 行降到 24 行，指数和 synthetic feed 不再进入个股模型。
- `STAGE_EXIT_FIRST` 和 `AMBIGUOUS_SAME_BAR` 按新事件语义映射为 `NO`；缺行情和未到期仍排除。US 推进因此从 3 行增加到 5 行。
- 所有六项仍为 `INSUFFICIENT_EVIDENCE`：标的数不足、测试行数不足、支持档覆盖为 0，且至少一个基线未改善。没有伪造 `APPROVED`。
- 输出模型带 `study_mode=RETROSPECTIVE`、`operational_allowed=false` 和 `symbols`，注册表拒绝把回顾性批准模型作为操作性发布。

复算目录：`reports/prediction/outlook/review-holdout-{cn,us}-{daily,weekly}`、`review-holdout-*-daily-final` 和 `reports/prediction/outlook/review-score{2,3}-*`。这些目录属于生成产物，不进入版本库。

## 修复后重新冻结

用当前工作树重新生成开发评估，并按冻结候选的 market/asset/feed/adjustment/timeframe 和 `outlook_slot` 重新训练：

| 候选 | 训练/校准/测试 | Brier | 基线改善（频率/动量/价格） | 结果 |
| --- | --- | ---: | --- | --- |
| CN `OUTLOOK_TARGET_FIRST_5D_V1` | 72 / 65 / 46 | 0.1792 | +0.0538 / +0.0825 / −0.0543 | `INSUFFICIENT_EVIDENCE` |
| CN `ADVANCE_FIRST_5D_V1` | 1 / 10 / 2 | — | — | 正确作用域后训练仅 1 行，无法拟合；`INSUFFICIENT_EVIDENCE` |
| CN `CLOSE_BAND_2W_V1` | 4 / 33 / 102 | — | — | 正确作用域后训练段只有 4 行，无法拟合；`INSUFFICIENT_EVIDENCE` |
| US `CLOSE_BAND_2W_V1` | 88 / 95 / 57 | 0.5373 | −0.1172 / −0.1328 / −0.1216 | `INSUFFICIENT_EVIDENCE` |
| US `OUTLOOK_TARGET_FIRST_5D_V1` | 69 / 51 / 24 | 0.1311 | +0.1517 / +0.1340 / +0.0067 | `INSUFFICIENT_EVIDENCE` |
| US `ADVANCE_FIRST_5D_V1` | 19 / 6 / 7 | 0.3241 | −0.0139 / −0.0527 / −0.1233 | `INSUFFICIENT_EVIDENCE` |

US 日线使用最终代码重新生成的 `review-holdout-us-daily-final`；目标 24 行、推进 7 行。目标价格基线点估计为正，但 95% 区间下限仍不大于 0，且测试行数和标的数不足。

这验证了修复后的样本作用域确实会剔除 synthetic feed 和指数；也说明旧周线候选的高训练行数来自跨 feed 混样，不能继续用于批准。CN/US 日线候选使用最终代码重新生成的 `review-holdout-*-daily-final` 评分；六项仍全部为 `INSUFFICIENT_EVIDENCE`。

## 复现

```bash
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/prediction-outlook-history-dev-cn-v1.json \
  --output-dir reports/prediction/outlook/history-dev-cn
.venv/bin/python scripts/train_forecasts.py freeze \
  --protocol docs/protocols/prediction-outlook-history-dev-cn-v1.json \
  --decisions reports/prediction/outlook/history-dev-cn \
  --output reports/prediction/outlook/history-candidate-cn-target.json \
  --event OUTLOOK_TARGET_FIRST_5D_V1 --outlook-slot PRIMARY \
  --market CN --asset-type EQUITY --timeframe 1d --feed sina \
  --adjustment basis_date_qfq \
  --frozen-at 2026-03-31T23:59:59+00:00
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/prediction-outlook-history-holdout-cn-v1.json \
  --output-dir reports/prediction/outlook/history-holdout-cn
.venv/bin/python scripts/train_forecasts.py score \
  --candidate reports/prediction/outlook/history-candidate-cn-target.json \
  --holdout reports/prediction/outlook/history-holdout-cn \
  --output-dir reports/prediction/outlook/history-score-cn
```

合并多事件协议时，`train`/`freeze` 接受评估 manifest 的事件集合为训练协议的父集；训练仍只读取所选事件。`freeze` 对多队列协议要求显式 `--market/--asset-type/--timeframe/--feed/--adjustment`。

US 使用对应 `-us-` 协议和候选。

## 验证

```text
512 passed
ruff check / format / pip check: PASS
```

历史 holdout 取代了等待未来行情的阻塞；未来前向队列仍保留，但不是本轮完成条件。
