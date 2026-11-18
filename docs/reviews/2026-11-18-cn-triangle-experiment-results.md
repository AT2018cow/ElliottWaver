# A 股三角形子浪规则对照实验结果

日期 2026-11-18。实验协议见 [协议](2026-11-18-cn-triangle-experiment-protocol.md)。源码指纹 `bdd9820ba2c3d478c3b699841f16af4f319b274679ff41de6fbe36f734b169a5`，引擎 `ew-rules-v3/zigzag-v1/score-v2/subwave-v4/projection-v4/report-v4/search-v2/0.1.0`。本文件保留首次试跑；后续正式认证与修正小回放见 [复核结果](2026-11-18-cn-triangle-experiment-followup-results.md)。运行目录：

- 严格缺口模式：[reports/cn-triangle-experiment](../../reports/cn-triangle-experiment/)
- 日线 allow-gaps 敏感性：[reports/cn-triangle-experiment-allow-gaps](../../reports/cn-triangle-experiment-allow-gaps/)

## 结论

**结构规则上，经典口径更准确；价格结果上，本实验未分出优劣。**

- 经典组要求至少四条腿是已验证的锯齿族结构，复杂腿最多一条。五条平台同时填满三角形时，经典组拒绝，宽松组给出 `VERIFIED` 和满额 15 分。后者不符合 Frost/Prechter 对收敛三角形的内部要求。
- A 股 2023–2025 固定样本中，经典组和宽松组选出的信号完全相同；没有出现五平台或“三条锯齿 + 两条复杂腿”的实际入选三角形。因此价格结果无法证明宽松组更好。
- 嵌套 E 腿在真实样本中不是纯理论边缘：88 个 E 腿嵌套对中，允许嵌套后 `VERIFIED` 从 25 增至 40，`UNKNOWN` 从 51 降至 38，新增 15 个父级关联。它只改变父级标签，不改变本次信号或价格结果。

## 样本与运行

| 项目 | 设置 |
| --- | --- |
| 日线样本 | 4 个指数 + 11 只个股；沪深主板、科创板、创业板各 2–3 只 |
| 周线样本 | 4 个指数 + 4 只个股 |
| 决策窗口 | 2023-01-02 至 2025-12-31 |
| 日线决策 | 每周最后一个交易日；未来 20 个交易日 |
| 周线决策 | 完整交易周；未来 8 个完整周 |
| 搜索/评分 | 两组共用同一快照、锚点、搜索预算、`max_subwave_depth=1`、评分权重和 Top-K |
| 事件边界 | 决策收盘价上下各 1×ATR(14)，方向取 D→E 的反方向 |
| 样本限制 | 2–3 只/板块，不是协议建议的 10–20 只；属于固定样本 pilot，存在幸存者偏差 |

严格模式下日线有 1095/2309 个决策因未知缺口被排除；`--allow-gaps` 敏感性运行全部 2309 个决策可分析。周线有 63 个决策因科创板历史不足被排除。

## 主结果

### 日线

| 指标 | 经典组 | 宽松组 |
| --- | ---: | ---: |
| 决策时点 | 2309 | 2309 |
| 去重信号 | 6 | 6 |
| 覆盖率 | 0.26% | 0.26% |
| `TARGET_FIRST` | 4 | 4 |
| `ADVERSE_FIRST` | 2 | 2 |
| 每 100 个决策时点先达目标 | 0.173 | 0.173 |
| 条件命中率 | 66.7% | 66.7% |

日线 `--allow-gaps` 敏感性：

| 指标 | 经典组 | 宽松组 |
| --- | ---: | ---: |
| 决策时点 | 2309 | 2309 |
| 去重信号 | 11 | 11 |
| `TARGET_FIRST` | 7 | 7 |
| `ADVERSE_FIRST` | 4 | 4 |
| 每 100 个决策时点先达目标 | 0.303 | 0.303 |
| 条件命中率 | 63.6% | 63.6% |

### 周线

| 指标 | 经典组 | 宽松组 |
| --- | ---: | ---: |
| 决策时点 | 1230 | 1230 |
| 去重信号 | 6 | 6 |
| `TARGET_FIRST` | 3 | 3 |
| `ADVERSE_FIRST` | 3 | 3 |
| 每 100 个决策时点先达目标 | 0.244 | 0.244 |
| 条件命中率 | 50.0% | 50.0% |

### 分歧与门槛

```text
严格日线：neither=3527，both_same=12
allow-gaps 日线：neither=3522，both_same=17
classic_only=0，loose_only=0，both_different=0
```

协议预设门槛是至少 50 个去重分歧信号、主要指标至少增加 1 个/100 决策、配对 95% 区间下界大于 0。本次分歧信号为 0，配对差值恒为 0，区间为 `[0, 0]`，因此判定为 **undetermined**。这不是“两组等价”的证明，而是当前样本量和信号数不足以区分价格表现。

## 合成结构对照

运行 `scripts/acceptance_triangle_children_20261118.py`，预期退出 1：

| 场景 | 当前宽松口径结果 | 经典口径预期 |
| --- | --- | --- |
| 五条平台 | `VERIFIED`，fit 1.0，15 分 | 拒绝整体认证 |
| 五条锯齿 | `VERIFIED`，fit 1.0，15 分 | 通过 |
| E 腿嵌套三角形 | `UNKNOWN` | 有证据时进入认证 |

这说明宽松口径的主要风险不是“多找到几个候选”，而是会把五平台这种不符合收敛三角形内部要求的结构算成满额子浪证据。该分数会进入质量和候选权重。

## 嵌套 E 腿

| 指标 | 不允许嵌套 | 允许嵌套 |
| --- | ---: | ---: |
| E 腿精确嵌套对 | 88 | 88 |
| `VERIFIED` | 25 | 40 |
| `PROVISIONAL` | 12 | 10 |
| `UNKNOWN` | 51 | 38 |

新增关联示例包括深证成指、贵州茅台、中芯国际、宁德时代等日线样本。允许嵌套 E 腿不会改变本次信号选择，因为父级上下文当前不参与评分；它改善的是父级关联覆盖率和标注完整性。

## 判定

1. **T-R01 平台腿与整组门槛：经典口径更准确。** 真实样本没有价格差异，但经典口径拒绝五平台满额认证，符合理论规则。建议把“至少四腿锯齿族、复杂腿最多一条”作为三角形结构认证默认；宽松逐腿口径只作为候选筛选变体，不替代经典认证标签。
2. **T-R02 嵌套 E 腿：经典/EWI 口径更完整。** 真实样本出现 88 个嵌套对，允许后新增 15 个 `VERIFIED` 父级关联。建议把 E 腿嵌套作为父级上下文扩展恢复，明确标注实验性或单独版本；它不应改变评分，除非另做单变量评分实验。
3. **价格表现：本实验未分出优劣。** 分歧信号为 0，远低于 50 的门槛；不能据此声称经典组或宽松组预测更准。

## 复现

```bash
.venv/bin/python scripts/acceptance_triangle_children_20261118.py \
  --output reports/cn-triangle-experiment/preflight-triangle.json
.venv/bin/python scripts/acceptance_parent_subwave_20261118.py \
  --frozen --output reports/cn-triangle-experiment/preflight-p3.json
.venv/bin/python scripts/experiment_cn_triangle_20261118.py \
  --output-dir reports/cn-triangle-experiment
.venv/bin/python scripts/experiment_cn_triangle_20261118.py \
  --allow-gaps --output-dir reports/cn-triangle-experiment-allow-gaps
```

产物包含 `protocol.json`、`manifest.json`、`decisions.jsonl`、`events.csv`、`divergences.json`、`nested_annotation.json`、`summary.json` 和 `conclusion.md`。所有结果基于冻结快照；未评估交易收益、滑点、涨跌停可执行性或概率校准。
