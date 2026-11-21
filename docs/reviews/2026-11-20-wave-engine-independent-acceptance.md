# 多浪级引擎独立验收清单

本清单按[多浪级波浪引擎实施计划](../WAVE_ENGINE_IMPLEMENTATION_PLAN.md)的 Codex 验收门槛逐项记录。`0.2.1` 源码、冻结协议和合成快照为证据；Codex 独立复核尚未执行，状态为 `PENDING_EXTERNAL_CODEX`。

## 总表

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| 规则真值：经典规则 | 通过 | `tests/test_engine.py`、`tests/test_variant_end_to_end.py`、规则矩阵测试 |
| WE-1 逐腿结构树 | 通过（核心） | `match_interval` 正例/反例/预算；结构树测试 |
| WE-2 日周父子匹配 | 通过（核心+边界） | `link_timeframes` 精确腿、拆股可比/不可比、价格口径、等高极值、缺交易日、早收/时区、两种父候选、周浪 2 三角形、子浪证据、亚型准入和历史父腿 `match_interval` 测试；高级 `brief` 的 `wave_relations` |
| WE-3 亚型正式入口 | **通过（正式入口+内部结构）** | 9 个冻结合成快照均从正式 `analyze`/`brief` 产生预期 `variant`；引导/终结楔形内部 `3-3-3-3-3` 结构树 `VERIFIED` |
| WE-4 14 拐点穷举 | 通过 | `tests/test_exhaustive_search.py::test_fourteen_pivot_search_matches_independent_exhaustive_reference` |
| WE-4 多窗口比较 | 通过 | `BriefWindowComparison.participants`；`test_brief_compares_multiple_automatic_windows` |
| WE-4 候选召回 | 通过 | 14 拐点穷举召回 classic 54.0% / advanced 100.0%；CN 固定样本覆盖 65.6%→82.0%，US 88.5%→100.0%；`scripts/compare_candidate_recall.py` |
| WE-4 性能 | 通过 | 30 决策 1.86×、60 决策 1.89×，`default_gate_2x=PASS` |
| WE-5 决策比较 | 通过 | `compare_decisions()`、评估路径、`HARD_INVALIDATED/EVIDENCE_CHANGED/SEARCH_LIMITED/TIP_MOVED` 测试 |
| 数据因果与版本 | 通过 | 经典配置哈希保持；旧 JSON 校验测试；前缀回放测试 |
| 可安装与使用 | 通过（离线） | 415 项全量测试、Ruff、离线 `analyze`/`brief` |
| Codex 独立验收 | **待外部执行** | 本文件即提交给 Codex 的清单 |

## WE-3 逐亚型正式入口

使用 `docs/protocols/wave-engine-variant-fixtures.json` 的冻结合成快照，运行未 monkeypatch 的：

```bash
.venv/bin/eliot analyze <symbol> --offline --snapshot <id> \
  --config config.variant.example.yaml --format json --json
.venv/bin/eliot brief <symbol> --offline --snapshot <id> \
  --config config.variant.example.yaml --no-weekly --no-window-check --json
```

高级配置使用 `scales=[0.5,1.0,1.4,2.4,3.8,8.0]`、`max_subwave_depth=2`、`beam_width=1024`、`max_bridge_intervals=21`、`min_quality=0`、`min_coverage=0.1`。`8.0` 只用于把双重三浪的父级 WXY 与内部平台分开；`beam_width/max_bridge_intervals` 只用于保留长父腿；默认高级配置仍为 `1.0/1.4/2.4/3.8`。`leading_diagonal` 使用快照中的 `anchor` 字段把楔形起点作为显式锚点，使父推动和楔形同时进入搜索域。

| 亚型 | 快照 | 正式入口观察 | 状态 |
| --- | --- | --- | --- |
| `REGULAR_FLAT` | `flat_regular` / `900001.SZ` | `analyze` 与 `brief` 均出现 `REGULAR_FLAT`；平台 COMPLETE 卡进入主备 | 通过 |
| `EXPANDED_FLAT` | `flat_expanded` / `900002.SZ` | `analyze` 与 `brief` 均出现 `EXPANDED_FLAT`；平台 COMPLETE 卡进入主备 | 通过 |
| `RUNNING_FLAT` | `flat_running` / `900003.SZ` | `analyze` 与 `brief` 均出现 `RUNNING_FLAT` | 通过 |
| `BARRIER_TRIANGLE` | `barrier_triangle` / `900004.SZ` | `analyze` 与 `brief` 均出现 `BARRIER_TRIANGLE` | 通过 |
| `TRUNCATED_FIFTH` | `truncated_fifth` / `900005.SZ` | `analyze` 与 `brief` 均出现 `TRUNCATED_FIFTH`，且为主选 | 通过 |
| `CONTRACTING_DIAGONAL` | `contracting_diagonal` / `900006.SZ` | `analyze` 与 `brief` 均出现 `CONTRACTING_DIAGONAL` | 通过 |
| `LEADING_DIAGONAL` | `leading_diagonal` / `900009.SZ` | 父腿 `IMPULSE/1`（`VERIFIED`）；楔形结构树 `VERIFIED`，五条腿均认证为 `ZIGZAG_ABC`（3-3-3-3-3）；`analyze` 与 `brief` 均出现 `LEADING_DIAGONAL` | 通过 |
| `ENDING_DIAGONAL` | `ending_diagonal` / `900008.SZ` | 父腿 `ZIGZAG_ABC/C`（`VERIFIED`）；楔形结构树 `VERIFIED`，五条腿均认证为 `ZIGZAG_ABC`（3-3-3-3-3）；`analyze` 与 `brief` 均出现 `ENDING_DIAGONAL` | 通过 |
| `DOUBLE_THREE` | `wxy_double_three` / `900007.SZ` | WXY 的 W=`FLAT`、X/Y=`ZIGZAG_ABC`，结构树 `VERIFIED`；`analyze` 与 `brief` 均出现 `DOUBLE_THREE`，且为主选 | 通过 |

补充说明：

- `LEADING_DIAGONAL` 的父级位置由 `VARIANT_PARENT_POSITIONS` 限制为 `IMPULSE/1`、`ZIGZAG_ABC/A`、`FLAT/A`、`WXY/W`；`ENDING_DIAGONAL` 限制为 `IMPULSE/5`、`ZIGZAG_ABC/C`、`FLAT/C`、`WXY/Y`。未知父级仍保留通用楔形几何证据。
- `DOUBLE_THREE` 的验收口径为 W 允许锯齿或平台、X 为许可调整结构、Y 允许锯齿/平台/三角形，且 W/Y 中锯齿至多一个。该快照用平台–X–锯齿，结构子节点由 `build_structure` 在候选点自身的尺度以下认证。
- `brief` 的主备卡选择现在优先展示已分类 `variant` 的同级备选，避免合格亚型因前两张普通候选卡而被截断。
- `allowed_child_templates_advanced("DIAGONAL", ...)` 新增内部许可表：终结楔形五腿均为 `ZIGZAG_ABC/FLAT/WXY`（3-3-3-3-3）；引导楔形 1/3/5 腿额外允许 `IMPULSE/DIAGONAL`（5-3-5-3-5），2/4 腿仍为调整结构。`tests/test_hierarchical_engine.py` 覆盖许可表和 5-3-5-3-5 的 `match_interval` 认证。
- 回归测试 `tests/test_variant_fixtures_end_to_end.py` 对同一组 9 个快照同时断言 `analyze` 和 `brief` 的 `variant`。

结论：**WE-3 的 9 个验收亚型均已从正式入口产生，`analyze` 与 `brief` 一致；引导/终结楔形的内部 3-3-3-3-3 结构树均为 `VERIFIED`，引导的 5-3-5-3-5 许可表由单元测试覆盖。** WE-3 可以标为通过。默认引擎仍为 `classic_v4`，高级模式保持显式 opt-in，待 Codex 独立复核后再决定默认切换。

## 独立 Codex 复核项

1. 用同一冻结合成快照复跑上表命令，核对 `variant`、`structure`、`conditions` 和报告 JSON。
2. 对每个亚型给出独立反例，确认不是路径或评分阈值造成的假阳性；重点复核 `LEADING/ENDING_DIAGONAL` 的父级位置、内部 3-3-3-3-3/5-3-5-3-5 组合和 `DOUBLE_THREE` 的 W/X/Y 子结构。
3. 核对高级模式 `brief` 的 `wave_relations` 与单周期 `analyze` 的子节点一致。
4. 核对 30/60 决策性能基准和 `default_gate_2x`。
5. 决定是否在修复后切换默认；当前默认仍为 `classic_v4`。

## 证据

- `docs/reviews/2026-11-20-wave-engine-acceptance.md`
- `docs/reviews/2026-11-20-wave-engine-acceptance-evidence.json`
- `docs/protocols/wave-engine-variant-fixtures.json`
- `reports/wave-engine/variants/summary.json`
- `reports/wave-engine/variants/*-analyze.json`
- `reports/wave-engine/variants/*-brief.json`
- `reports/wave-engine/acceptance/`
- `reports/wave-engine/benchmark/`
- `reports/wave-engine/recall/`
