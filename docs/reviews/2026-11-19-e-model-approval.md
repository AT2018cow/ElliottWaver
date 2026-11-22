# E 扩大样本训练、校准与审批记录

日期 2026-11-19。范围：E 的扩大样本评估、训练/校准、正式审批，以及默认模型注册和回退验证。A–D、F、G 见其他验收记录。

协议：[`docs/protocols/e-expanded-2023-2025.json`](../protocols/e-expanded-2023-2025.json)。协议在查看扩大样本留出结果前冻结；样本为本地可用的 16 个日线标的（11 只沪深个股、5 个沪深指数）和 8 个周线标的（5 个原生周线、3 个由长历史日线派生）。

证据：[`2026-11-19-e-model-approval-evidence.json`](2026-11-19-e-model-approval-evidence.json)（协议哈希、评估汇总、模型汇总和审批结论）。

源码指纹 `674a29f447032a528416a9604148380f34cd88fbbd029b4deded888a19d31d0c`（E 运行基线；D 结构指标为后续增量）；完整测试 219 项通过。

## 发布门槛

- 相对类别频率、20 根动量、完整价格组三项基线，Brier 和 log loss 改善的配对 95% 区间下界均大于 0。
- 每个展示可靠性档至少 50 个成熟观测、10 个不同决策日期，预测均值减实际发生率的 95% 区间在 ±0.10 内。
- 测试样本至少 50 行、10 个日期、5 个标的。
- 支持档覆盖率至少 0.5。
- 按共同决策日期做连续时间块配对重抽样，2000 次，固定 seed `20261119`；块长覆盖事件期限。
- 线上三分类任何一个分量落入未获支持的档位时，整组概率置空。

## 复现

```bash
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/e-expanded-2023-2025.json \
  --output-dir reports/eval-expanded

# 单样本分批评估（可选，避免长进程被终止）
ARGS=()
for part in reports/eval-parts/part-*; do ARGS+=(--input "$part"); done
.venv/bin/python scripts/merge_evaluations.py \
  --protocol docs/protocols/e-expanded-2023-2025.json \
  "${ARGS[@]}" --output-dir reports/eval-expanded

.venv/bin/python scripts/train_forecasts.py \
  --protocol docs/protocols/e-expanded-2023-2025.json \
  --decisions reports/eval-expanded \
  --output-dir reports/models-expanded
```

## 评估样本

合并 24 个单样本评估结果（16 个日线 + 8 个周线）：

```text
decisions        3695
analyzable       3458
excluded         237（INSUFFICIENT_DATA）
日线             2463 decisions / 2446 analyzable
周线             1232 decisions / 1012 analyzable
资产类型         EQUITY 2463 / INDEX 1232
```

事件行数：

```text
CLOSE_BAND_5D_V1     2446
CLOSE_BAND_20D_V1    2446
CLOSE_BAND_5W_V1     1012
CLOSE_BAND_20W_V1    1012
CONFIRM_FIRST_20D_V1 1683，成功 131，可判定 495
TARGET_FIRST_20D_V1  1683，成功 271，可判定 755
CONFIRM_FIRST_8W_V1   680，成功 52，可判定 263
TARGET_FIRST_8W_V1    680，成功 126，可判定 369
```

周线样本的可分析率较低（1012/1232），主要原因是 600 根周线窗口下部分日期没有合格结构；这些决策仍进入分母，只是不产生结构事件。

## 训练与审批结果

模型 `model_id = cbe2c3e6c839831c49c7f20af194af3b7d199139bc300bd7c15acd2b1e57338d`。

```text
事件                    状态                 训练/校准/测试     测试日期  标的  支持档覆盖率
CLOSE_BAND_5D_V1        INSUFFICIENT_EVIDENCE  1200/624/618       40       16    0.000
CLOSE_BAND_20D_V1       INSUFFICIENT_EVIDENCE  1200/624/618       40       16    0.000
CLOSE_BAND_5W_V1        INSUFFICIENT_EVIDENCE   459/273/280       40        7    0.000
CLOSE_BAND_20W_V1       INSUFFICIENT_EVIDENCE   459/273/262       40        7    0.000
CONFIRM_FIRST_20D_V1    INSUFFICIENT_EVIDENCE   190/194/111       40       15    0.000
TARGET_FIRST_20D_V1     INSUFFICIENT_EVIDENCE   323/270/162       40       15    0.000
CONFIRM_FIRST_8W_V1     INSUFFICIENT_EVIDENCE    90/121/52        38        6    0.000
TARGET_FIRST_8W_V1      INSUFFICIENT_EVIDENCE   140/153/76        38        6    0.000
```

Brier 对照（越低越好）与改善的 95% 区间：

```text
事件                  模型     频率     动量     价格     Brier 改善 CI（频率 / 动量 / 价格）
CLOSE_BAND_5D_V1      0.6379   0.6344   0.6337   0.6368   [-0.0121,0.0061] / [-0.0128,0.0049] / [-0.0048,0.0025]
CLOSE_BAND_20D_V1     0.6914   0.7378   0.6854   0.6882   [0.0268,0.0647] / [-0.0102,-0.0020] / [-0.0065,-0.0001]
CLOSE_BAND_5W_V1      0.7120   0.7052   0.6893   0.6925   [-0.0619,0.0569] / [-0.0333,-0.0130] / [-0.0306,-0.0103]
CLOSE_BAND_20W_V1     0.7247   0.7543   0.7015   0.7045   [0.0152,0.0421] / [-0.0331,-0.0120] / [-0.0313,-0.0074]
CONFIRM_FIRST_20D_V1  0.1974   0.1806   0.2053   0.2095   [-0.0767,0.0331] / [-0.0526,0.0559] / [-0.0460,0.0598]
TARGET_FIRST_20D_V1   0.2418   0.2306   0.2509   0.2486   [-0.0199,-0.0032] / [-0.0037,0.0199] / [-0.0050,0.0164]
CONFIRM_FIRST_8W_V1   0.2608   0.1496   0.1791   0.1713   [-0.1736,-0.0564] / [-0.1030,-0.0632] / [-0.0999,-0.0807]
TARGET_FIRST_8W_V1    0.3271   0.2220   0.2949   0.3062   [-0.1926,-0.0031] / [-0.0831,0.0405] / [-0.0634,0.0360]
```

log loss 的改善区间同样未通过：8 个事件相对动量/价格基线的区间下界均不大于 0；部分事件相对频率基线也不大于 0。

## 失败原因

1. **没有事件同时跑赢三项基线。** 20D/20W 收盘事件虽然略优于频率基线，但不如 20 根动量和价格组；5D/5W 收盘事件连频率基线也未跑赢。候选确认/目标事件中，`TARGET_FIRST_20D_V1` 相对动量/价格有正点估计，但 95% 区间跨 0。
2. **可靠性档位支持不足。** 以 `CLOSE_BAND_20D_V1` 为例：
   - `RANGE` 的 [0.2,0.4) 档有 618 个观测、40 个日期，预测均值 0.313、实际 0.282，区间 [-0.018,0.085]，通过；
   - `UP` 的 [0.2,0.4) 档预测 0.300、实际 0.490，区间 [-0.281,-0.103]，偏差超出 ±0.10；
   - `DOWN` 的 [0.2,0.4) 档预测 0.374、实际 0.236，区间 [0.044,0.215]；[0.4,0.6) 档预测 0.412、实际 0.211，区间 [0.105,0.290]。
   因此三分类每个预测都至少有一个分量落在未获支持档位，支持档覆盖率为 0；按设计整组概率应置空。
3. **二分类分档未通过。** `CONFIRM_FIRST_20D_V1` 测试 111 行，但最接近门槛的 `NO` [0.8,1.0) 档只有 46 个观测，未达到每档 50 个的要求。`TARGET_FIRST_20D_V1` 的 `NO` [0.4,0.6) 档有 102 个观测、39 个日期，但预测 0.558、实际 0.696，区间 [-0.242,-0.045]；`YES` [0.4,0.6) 档预测 0.442、实际 0.304，区间 [0.045,0.242]，均超出 ±0.10。8W 候选事件的最大分档只有 18/24 个观测，未达到 50 个。
4. **周线候选事件样本更少。** 8W 候选事件测试只有 52/76 行；可靠性分档无法达到发布门槛。

## 结论

**证据不足，未注册默认批准模型。**

- 8 个事件全部为 `INSUFFICIENT_EVIDENCE`，没有事件满足设计发布门槛。
- `ModelRegistry.register` 拒绝该模型：`MODEL_INVALID: no APPROVED event`。
- 默认注册表 `.cache/eliotbot/models` 未创建；`brief` 不加载任何模型时保持 `UNCALIBRATED`。
- 显式 `--model reports/models-expanded/model.json` 时，`brief` 显示 `forecast_status=INSUFFICIENT_EVIDENCE`，每个事件 `probabilities=null`，并显示失败原因。
- `watchlist --model ... --workers 2 --retries 1` 对 `600519.SH`、`000300.SH` 均返回 `OK`，报告中的事件状态为 `INSUFFICIENT_EVIDENCE`，不输出概率。

这符合设计中的“未获支持的项目交付验证结论，收盘摘要继续可用”。不把候选权重或未校准输出写成成功概率。

## 烟测输出

```text
with-model forecast_status INSUFFICIENT_EVIDENCE
  CLOSE_BAND_20D_V1 INSUFFICIENT_EVIDENCE None
  CLOSE_BAND_5D_V1  INSUFFICIENT_EVIDENCE None
  CONFIRM_FIRST_20D_V1 INSUFFICIENT_EVIDENCE None
  TARGET_FIRST_20D_V1  INSUFFICIENT_EVIDENCE None
no-model forecast_status UNCALIBRATED
register_rejected: MODEL_INVALID: no APPROVED event

watchlist 2 条记录：600519.SH OK，000300.SH OK
```

## 边界

- 本地可用样本为 11 只沪深个股 + 5 个沪深指数（日线），未达到设计建议的约 30 只分层个股；结论只适用于该样本，不能外推为全市场已校准概率。
- 周线 1232 个决策中 220 个为 `INSUFFICIENT_DATA`，结构事件分母进一步缩小。
- 本次只验证“是否发布概率”；决策时模型可用性和回顾性重算已在 [E 决策时模型记录](2026-11-19-e-decision-time-models.md) 单独验收。
- 扩大样本没有产生可批准模型；若后续增加样本或改进特征，应按同一协议重新冻结留出并复验，不能直接用本次测试集调参后再次声称通过。
- 所有改动未提交，等待验收。
