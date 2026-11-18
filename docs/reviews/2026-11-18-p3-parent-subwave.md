# P3 父级上下文与子浪规则认证

日期 2026-11-18。范围是 `867fe59` 之后的 P3 工作：当前证据收口、父级上下文、内部子浪规则认证。A 股为主，不包含北交所个股接入。

## 实现结果

| 能力 | 当前行为 |
| --- | --- |
| 父级上下文 | 候选完整落在更高浪级的某一父腿内，且方向一致时，标记 `parent_context=VERIFIED`；端点完全对齐时 `parent_relation=EXACT_LEG`，否则为 `CONTAINED`。父级最后一条腿仍在进行中时标记 `PROVISIONAL`。同时输出 `parent_pattern`、`parent_degree`、`parent_wave_label` 和 `parent_scenario_id`。 |
| 子浪规则认证 | 父腿端点加更细尺度确认拐点组成子候选，复用顶层硬规则检查 5 浪推动/楔形与 3 浪锯齿/平台。全部已确认腿通过才为 `VERIFIED`；有腿失败或进行中为 `UNVERIFIED`；没有足够细尺度证据为 `UNAVAILABLE`。 |
| 深度 2 | 在深度 1 已认证的子结构上，再用下一层更细尺度认证一层。缺少下一层证据时保留深度 1 结果，不把“无证据”当成失败。 |
| 默认与覆盖 | `--long-term` 默认启用深度 1；`--subwave-depth 0/1/2` 可显式覆盖。配置和 CLI 都区分“省略”与显式 0。 |
| 报告 | JSON、Markdown、Rich 表格增加父级、子浪状态和逐腿证据。 |

引擎标识升级为：

```text
ew-rules-v3/zigzag-v1/score-v2/subwave-v3/projection-v4/report-v4/search-v2/0.1.0
```

## 复验

- **171 项测试通过**，42 条告警来自第三方 timedelta 弃用接口。Ruff lint/format 和 `pip check` 通过。
- 新增父级包含、精确腿、进行中父腿、跨父级枢轴拒绝、5-3-5 子浪认证、区间外拐点不影响深度 2 等测试。
- 21 组冻结真实快照重算并校验 JSON/MD/HTML 摘要与文件哈希，覆盖 5 组长历史、7 组指数与个股回归、9 组多周期样本。
- 10 个长历史前缀的 API 回放与单点分析摘要一致。
- 六项 ENG-R01 至 ENG-R06 反例继续通过。

长历史 5 组、每组 Top-8，共 40 个展示候选：

| 指标 | 数量 |
| --- | ---: |
| 父级 `VERIFIED` | 12 |
| 父级 `PROVISIONAL` | 1 |
| 父级 `UNKNOWN` | 27 |
| 子浪 `VERIFIED` | 3 |
| 子浪 `UNVERIFIED` | 29 |
| 子浪 `UNAVAILABLE` | 8 |

上证长历史中，`2024-02-05 → 2026-05-11` 的双锯齿 WXY 获得 `CYCLE` 三角形 E 腿的 `EXACT_LEG` 父级关系，三条腿均通过 3 浪锯齿规则，子浪状态为 `VERIFIED`。另一组 `2019-01-02 → 2026-05-11` 的 WXY 获得 `SUPERCYCLE` 三角形 E 腿的 `EXACT_LEG` 关系并通过子浪认证。说明认证不是“全部通过”，而是能区分真正通过的结构和证据不足的结构。

## 当前证据

- [ext5 v3](../validation_20261118_ext5_v3.json)：9 组个股多周期。
- [沪深京指数 v3](../validation_20261118_cn_index_v3.json)：7 组指数与个股回归。
- [长历史 v3](../validation_20261118_long_history_v3.json)：5 组 1991–2026 周线。
- [引擎修复复验证据](2026-11-18-engine-fix-evidence.json)：21 组冻结样本与 6 项反例。

## 边界

- 父级关系只在本次枚举到的候选之间建立；搜索未枚举到的更大结构不能凭空补造，仍为 `UNKNOWN`。多个父级都包含候选时，优先选择最窄的父腿。
- 子浪认证是规则级认证，不是无限递归的完整 Elliott 分解；每个父腿最多保留 12 个内部拐点，超过时等距取样，组合搜索有界。复杂组合调整、running flat、扩散楔形和截短五浪仍按未覆盖处理。
- 浪级仍是 K 线跨度启发式；父级关系要求严格包含，不用形态相似度猜父级。
- 预测效果和概率校准尚未评估；`relative_weight` 仍不是成功概率。

## 复现

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/acceptance_engine_20261118.py \
  --output docs/reviews/2026-11-18-engine-fix-evidence.json
```

长历史 v3 证据使用本地冻结快照：

```bash
.venv/bin/python scripts/validate_snapshots.py \
  --cache-root .cache/eliotbot \
  --long-term --allow-gaps \
  --case sse_long:6f21546d3f70ccc998a0c08eccec30b56b30a70df37c31237e6ad79f29e47ec6:1w \
  --output docs/validation_20261118_long_history_v3.json \
  --reports reports/validation-20261118-long-history-v3
```
