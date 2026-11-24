# 单指数交易指导 E 批实施与复验

E 批覆盖 TD-15。D 批审查修复已经先提交为 93bf7a8，31 项定向测试和全量 670 项测试通过。 [D 批审查修复](2026-11-25-single-index-batch-d-review-repair.md) · [盘中协议、模板和命令](../protocols/README.md#单指数盘中概率研究)

| 验收范围 | 实测 |
| --- | --- |
| 标签及日历 | 1m、5m、15m、30m、60m 各核对推进和目标两类事件，每类分别跑 PRIMARY、ALTERNATE_1、ALTERNATE_2。30 组夹具核对周五末段至周一开盘的五个准确观察区间，并测试成功、失效、到期、同根冲突、缺口及迟到收盘。 |
| v5 特征及兼容 | 现价 100、ATR 2 时，104 为 +2 ATR，96 为 −2 ATR。测试区分恰为 100 与缺条件，并验证比较符、背景实际周期、方向冲突以及价格基线共同几何。旧 v2、v3、v4 特征调用继续通过既有回归。 |
| 切分与留出 | 同一市场日的多个决策以精确 bar 身份评分，置信区间按市场会话聚类。测试对比纽约时间同一晚和次日零时收到的标签，后者跨段剔除。只采用已收齐完整窗口的成熟标签，保留来源 bar 和计划到期。 |
| 冻结、注册和简报 | 受控时钟夹具运行开发训练、冻结、预封存、发行回执读取、前向评分、注册表与简报。SPX 单指数夹具被判定为证据不足，操作性注册拒绝。另用合成的已批准模型测试应用路径，验证周期、feed、标的、卡片、批准区间和到期日。 |

## 真实历史复算

两组 Yahoo SPX 冻结快照用同一开发窗口 2026-09-08 至 2026-10-16 和独立留出发行窗口 2026-10-20 至 2026-11-05 回放，分别使用对应的分钟输入和 PRIMARY 目标先达事件。按 HISTORICAL_HOLDOUT 评分。期后的冻结快照包含标签观察资料，发行均按历史截断点重放。

| 快照 | 结果 |
| --- | --- |
| 15m c60241486915f386c6095f0ccc475f27fa2fcecd11c50e844c9bfe2e522c6aee | 开发 32 格、留出 14 格均因所选时点存在尚未可用的尾部行情而判为 STALE_DATA。留出成熟标签 0，输出 INSUFFICIENT_EVIDENCE。 |
| 60m 83dda4e66fa2483fca09976dcc104653bf2e54379bf743eab41bf24893d159ab | 开发 34 格、留出 15 格。留出共 13 条事件记录，PRIMARY 有 8 条成熟目标事件。开发训练及校准样本不足以拟合，评分输出 INSUFFICIENT_EVIDENCE。 |

用同一冻结评分产物调用正常的 60m 离线简报入口，PRIMARY 卡显示 5B 目标先达事件及 INSUFFICIENT_EVIDENCE。两份协议、快照身份、发行格、事件表、候选和评分保存在本机 reports/intraday-probability-validation/2026-11-25-spx-v5/。以 60m 为例，在工作区运行：

```bash
probe=reports/intraday-probability-validation/2026-11-25-spx-v5
.venv/bin/python scripts/evaluate_briefs.py --protocol "$probe/60m-development.json" --output-dir "$probe/60m-development"
.venv/bin/python scripts/evaluate_briefs.py --protocol "$probe/60m-holdout.json" --output-dir "$probe/60m-holdout"
.venv/bin/python -m scripts.train_forecasts freeze --protocol "$probe/60m-development.json" \
  --decisions "$probe/60m-development" --output "$probe/60m/candidate.json" \
  --outlook-slot PRIMARY --event OUTLOOK_TARGET_FIRST_60M_5B_V1
.venv/bin/python -m scripts.train_forecasts score --candidate "$probe/60m/candidate.json" \
  --holdout "$probe/60m-holdout" --output-dir "$probe/60m/score"
```

## 代码审查修复

| 审查范围 | 定向复验 |
| --- | --- |
| 行情覆盖和实际收到时间 | 前向测试保留提供商的请求截止，整批结果在其后 30 秒收到。协议截止已覆盖收到时间时，完整窗可核销，并在模型记录中保留实际最晚标签时刻。 |
| 指数专属校准 | 回测保留每个指数的可靠性分箱。合成 SPX 的 YES 0.70 通过自身分箱，0.90 落入仅合并样本支持的档位，简报显示未支持且隐藏概率。 |
| 留出时间截断 | 上海到期 03:30 UTC 的同一标签用带时区和纯日期截止分别核算。纽约当地时间前一晚 23:50 对应更晚的实际时刻，标签计入；较早的时刻和前一日日期截止均排除。 |
| 模型批准 | 末笔标签次日 01:00 UTC 才收到时，拒绝 00:00 UTC 批准。到达证据时刻才允许批准及查询。 |
| 实时简报到期 | 初算后到期时，即使卡片和观察期相同也重新筛选模型。另在发行回执写入时注入到期，CLI 重新生成，历史只登记最终有效的一份。 |

## 测试环境及证据范围

```bash
.venv/bin/python -m pytest tests/test_intraday.py tests/test_intraday_forecast_pipeline.py tests/test_outlook_forecast.py tests/test_prediction_freeze.py -q
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

审查修复的定向回归 103 项通过，最终全量 712 项测试通过。Ruff、依赖及差异检查通过。本地完成合成与真实冻结复算，并在仓库外用隔离安装的 wheel 核对包内分钟事件及 v5 顺序。真实操作性概率仍需封存后的新发行及成熟行情，满足独立测试样本、校准、三种基线和目标指数专项门槛后才能批准。前向采集可用本地主机或服务器，Modal 只在验收其自身调度部署时需要。
