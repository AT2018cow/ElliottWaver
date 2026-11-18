# A 股三角形规则复核与正式认证结果

日期 2026-11-18。前置复核见 [试跑复核与下一步](2026-11-18-cn-triangle-experiment-followup.md)，原始协议见 [实验协议](2026-11-18-cn-triangle-experiment-protocol.md)，首次试跑见 [试跑结果](2026-11-18-cn-triangle-experiment-results.md)。

源码指纹 `975dfd9dcc24b54b4562c79a94d4caf7ffab123ccd8088d458a19c576612afd4`，引擎：

```text
ew-rules-v3/zigzag-v1/score-v2/subwave-v5/projection-v4/report-v4/search-v2/0.1.0
```

## 正式结构认证

已完成收敛三角形现在使用经典整组门槛：

- 至少四条腿是已验证的锯齿族结构（`ZIGZAG_ABC` 或 `WXY`）。
- 复杂腿最多一条（`FLAT`、`WXY`、`TRIANGLE`）。
- `WXY` 同时计入锯齿族和复杂腿，避免两个 `WXY` 被误算成零条复杂腿。
- 五平台、三锯齿加两平台、双复杂腿不得整体 `VERIFIED`，也不得获得满额子浪分。
- 五锯齿和四锯齿加一平台可以通过。

定向验收：

| 场景 | 结果 |
| --- | --- |
| 五平台 | `UNVERIFIED`，fit 0，0/15 分 |
| 五锯齿 | `VERIFIED`，fit 1.0，15/15 分 |
| 三锯齿加两平台 | 不得整体 `VERIFIED` 或满额 |
| 普通推动第 3 浪楔形 | 仍为 `UNVERIFIED`，P3-R02 通过 |
| 锯齿 C 腿推动正例 | 仍为 `VERIFIED`，P3-R01 通过 |

## E 腿嵌套

E 腿嵌套三角形现在只形成候选提示：

- 允许按端点和方向建立关系。
- 精确匹配时 `parent_relation=EXACT_LEG`，但 `parent_context` 固定为 `PROVISIONAL`，不升级 `VERIFIED`。
- 父级标签不进入评分，评分仍使用原口径。
- 逐时点记录只保留实际选中的父候选 ID，并按标的、周期、父子端点去重。

6 日线 + 5 周线小回放结果：

| 指标 | 数值 |
| --- | ---: |
| 逐时点嵌套记录 | 10 |
| 去重唯一对 | 8 |
| 端点匹配 | 8 |
| 结构通过 | 8 |
| 端点且结构通过 | 8 |
| 新增 `UNKNOWN -> PROVISIONAL` | 5 |

上证 `2023-05-26` 子候选 `0118e3f1a3880efefd3d81e0` 只记录实际选中的父候选 `ee236239f2d70abaa45d2a1f`；另一父候选 `5ebe4e8825294484c23342c8` 不再被重复写成已关联。

## 6 日线 + 5 周线小回放

日线：4 个指数 + 贵州茅台 + 中国平安；周线：4 个指数 + 贵州茅台。

| 项目 | 日线 | 周线 |
| --- | ---: | ---: |
| 全网格 | 924 | 770 |
| 可分析 | 924 | 770 |
| 经典/宽松信号 | 4 / 4 | 4 / 4 |
| `TARGET_FIRST` | 3 | 2 |
| `ADVERSE_FIRST` | 1 | 2 |
| 每 100 可分析时点先达目标 | 0.325 | 0.260 |
| 条件命中率 | 75.0% | 50.0% |

```text
neither=1686，both_same=8
classic_only=0，loose_only=0，both_different=0
```

日线漏斗（各时点求和）：

```text
候选 54,929
完成收敛三角形 927
五腿全验证 144
经典/宽松整组通过 144 / 144
三角形评分达标 112 / 112
Top-K 三角形 43 / 43
信号 21 / 21
```

周线漏斗：

```text
候选 67,031
完成收敛三角形 4,610
五腿全验证 112
经典/宽松整组通过 112 / 112
三角形评分达标 77 / 77
Top-K 三角形 58 / 58
信号 56 / 56
```

Top-K 之前仍没有出现足够分歧，价格优劣继续维持 **未分出**。经典结构门槛作为正式认证默认；宽松口径只保留为候选筛选变体。

## 正式入口一致性

`scripts/validate_cn_triangle_formal.py` 检查 6 个决策点，全部通过：

- 可分析状态与 `AnalysisService` 一致。
- `diagnostics.candidates_found` 一致。
- 首个完整三角形 `scenario_id` 与实验经典组信号键一致。

## 回归

```text
176 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
21 组冻结样本                 PASS
ENG-R01 至 ENG-R06           PASS
P3-R01、P3-R02               PASS
```

## 产物

- 小回放与预检：[reports/cn-triangle-experiment-followup](../../reports/cn-triangle-experiment-followup/)
- `protocol.json`、`manifest.json`、`decisions.jsonl`、`events.csv`、`divergences.json`、`nested_annotation.json`、`summary.json`、`conclusion.md`
- `preflight-triangle.json`、`preflight-p3.json`、`formal-validation.json`

## 复现

```bash
.venv/bin/python scripts/acceptance_triangle_children_20261118.py \
  --output reports/cn-triangle-experiment-followup/preflight-triangle.json
.venv/bin/python scripts/acceptance_parent_subwave_20261118.py \
  --frozen --output reports/cn-triangle-experiment-followup/preflight-p3.json
.venv/bin/python scripts/validate_cn_triangle_formal.py \
  --output reports/cn-triangle-experiment-followup/formal-validation.json
.venv/bin/python scripts/experiment_cn_triangle_20261118.py \
  --max-daily 6 --max-weekly 6 \
  --output-dir reports/cn-triangle-experiment-followup
```
