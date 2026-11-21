# 多浪级波浪引擎高级配置首版验收

基线为 `0.2.1` 本地源码（`ew-rules-v4`，含 E 浪净回撤、浪 3 百分比长度和浪 5 失效价修复）。本轮交付 `engine_profile=hierarchical_v1` 的显式高级入口；默认仍是 `classic_v4`。默认配置哈希、旧报告字段和经典引擎行为保持不变。

## 结论

- **WE-1 通过（第一层结构树）**：新增 `match_interval()`，用父腿端点和更细尺度已确认拐点枚举允许子模板；支持 `VERIFIED/PENDING/SEARCH_LIMITED/REJECTED/UNRESOLVED`、预算截断和失败规则记录。高级候选带 `WaveStructure` 根节点、逐腿 `child_links` 和子节点。
- **WE-2 通过（日周父子匹配）**：新增 `link_timeframes()`，把周线父腿映射到完整周的日线极值，输出 `EXACT_LEG/CONTAINED/INCOMPATIBLE/UNDETERMINED` 和 `VERIFIED/PROVISIONAL`。新增日周价格口径、等高极值、缺交易日、子浪证据、亚型位置准入和历史父腿 `match_interval` 认证；`brief`/`watchlist` 的高级摘要带 `wave_relations`。
- **WE-3 通过（正式入口+内部结构）**：平台三变体、阻挡三角形、截短第五浪、收缩/引导/终结楔形和 WXY 双重三浪均已从冻结合成快照的正式 `analyze`/`brief` 产生预期 `variant`；完整父级位置矩阵 `VARIANT_PARENT_POSITIONS` 已接入服务层。引导/终结楔形新增内部许可表，五条腿均认证为 `ZIGZAG_ABC`（3-3-3-3-3），引导的 5-3-5-3-5 由单元测试覆盖。逐亚型证据见[独立验收清单](2026-11-20-wave-engine-independent-acceptance.md)。
- **WE-4 通过（结构/性能/大样本/召回）**：候选近似去重、共享极值查询命中计数、截断原因、6/8/14 拐点独立穷举、两个备选窗口的 `participants` 和验收协议已实现；新增 0.2.1 与 `hierarchical_v1` 的候选召回对照：14 拐点穷举召回 54.0% → 100.0%，CN 固定样本候选覆盖 65.6% → 82.0%，US 88.5% → 100.0%。高级模式 p95 为经典模式的 **1.86 倍**（60 决策 1.89 倍），性能门槛通过。
- **WE-5 通过**：提取 `compare_decisions()`，`attach_previous` 和评估网格共用结构状态比较；覆盖 `HARD_INVALIDATED`、`EVIDENCE_CHANGED`、`SEARCH_LIMITED`、`TIP_MOVED` 和条件回放（会话、bar ID、条件类别）。

## 验收样本

协议：

- `docs/protocols/wave-engine-acceptance-v1.json` / `wave-engine-acceptance-v1-advanced.json`
- `docs/protocols/wave-engine-acceptance-v1-us.json` / `wave-engine-acceptance-v1-us-advanced.json`

| 运行 | 引擎 | 资产 | 决策 | 可分析 | 信号覆盖 | 主选切换率 | 截断 | VERIFIED 主选 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CN classic | `classic_v4` | EQUITY | 128 | 72 | 41.7% | 37.0% | 0 | 0 |
| CN classic | `classic_v4` | INDEX | 50 | 50 | 100.0% | 79.2% | 0 | 0 |
| CN advanced | `hierarchical_v1` | EQUITY | 128 | 72 | 69.4% | 33.3% | 0 | 44 |
| CN advanced | `hierarchical_v1` | INDEX | 50 | 50 | 100.0% | 50.0% | 0 | 27 |
| US classic | `classic_v4` | EQUITY | 52 | 52 | 88.5% | 67.5% | 0 | 0 |
| US advanced | `hierarchical_v1` | EQUITY | 52 | 52 | 96.2% | 69.6% | 0 | 8 |

高级模式使用 `1.0` 细尺度和第一层认证；经典模式不启用新字段。US advanced 的 `VERIFIED` 主选由上一轮的 10 降为 8，原因是高级 WXY 子浪评分开始使用扩展许可后主选排序变化；结构状态仍按 `VERIFIED/PENDING` 分开记录。

## 关键证据

- 高级 CLI `brief 000016.SH`：`schema_version=1.2`、`engine_version` 带 `/hierarchical_v1`、`weekly_state=AVAILABLE`、8 条 `wave_relations`，主选结构 `VERIFIED`。
- 高级 CLI `analyze 000016.SH --timeframe 1w`：`schema_version=1.1`，5 个候选均带结构状态。
- 高级 CLI `brief AAPL`：20 条日周关系；`brief SPY --timeframe 1w`：两个主选结构 `VERIFIED`。
- `match_interval` 正例、ABC 反例、预算截断、平台/阻挡三角形/楔形/截短第五浪端点测试通过。
- 高级模式 178 个 CN 决策中股票 44/72、指数 27/50 主选结构 `VERIFIED`；US advanced 为 8/52。未验证候选保留 `PENDING`/`REJECTED`。

## 性能

`scripts/benchmark_wave_engine.py` 在 4 个 CN 样本的 30 个决策上，预热后 3 轮：

| 模式 | 决策数 | p50 | p95 |
| --- | ---: | ---: | ---: |
| `classic_v4` | 30 | 209.1 ms | 212.2 ms |
| `hierarchical_v1` | 30 | 393.4 ms | 394.6 ms |
| `classic_v4` | 60 | 189.7 ms | 189.9 ms |
| `hierarchical_v1` | 60 | 359.1 ms | 359.5 ms |

30 决策 p95 比值为 **1.86 倍**，60 决策为 **1.89 倍**，`default_gate_2x=PASS`。性能门槛已通过。默认决策仍保持 `classic_v4`，高级模式作为显式 opt-in 交付。

## 候选召回对照

`scripts/compare_candidate_recall.py` 用独立穷举枚举器计算 14 拐点固定样本召回，并用验收决策的 `candidate_keys` 统计固定样本候选覆盖。结果写入 `reports/wave-engine/recall/`。

| 配置/运行 | 参照候选 | 搜索找到 | 召回/候选覆盖 | 平均合格候选 | 截断 | 主选切换率 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 14 拐点 classic | 50 | 27 | 54.0% | — | 否 | — | — |
| 14 拐点 advanced | 50 | 50 | 100.0% | — | 否 | — | — |
| CN classic | — | — | 65.6% | 1.2 | 0 | 58.1% | 212.2 ms |
| CN advanced | — | — | 82.0% | 1.7 | 0 | 40.6% | 394.6 ms |
| US classic | — | — | 88.5% | 2.5 | 0 | 67.5% | — |
| US advanced | — | — | 100.0% | 3.0 | 0 | 70.0% | — |

14 拐点召回以独立枚举器为参照；经典配置使用默认 beam/bridge，高级配置使用固定大预算。固定样本候选覆盖 = 至少一个合格候选的决策 / 可分析决策。

## 复现

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/evaluate_briefs.py \
  --protocol docs/protocols/wave-engine-acceptance-v1-advanced.json \
  --output-dir reports/wave-engine/acceptance/advanced
.venv/bin/python scripts/benchmark_wave_engine.py \
  --protocol docs/protocols/wave-engine-acceptance-v1.json \
  --config config.advanced.example.yaml \
  --output reports/wave-engine/benchmark/advanced.json
.venv/bin/eliot brief 000016.SH --offline \
  --snapshot abfd7531e5f8e49a3cec1f27100d21cccc67c2d53c07c68e684fd0ac39c16a4f \
  --config config.advanced.example.yaml --json
```

验证结果：`415 passed`；Ruff 与格式检查通过。协议、对照、基准和 CLI 产物在 `reports/wave-engine/`（按仓库惯例不提交）。

## 后续维护项

1. WE-3：9 个亚型均已从正式入口产生；`tests/test_variant_fixtures_end_to_end.py` 对同一组冻结合成快照同时断言 `analyze` 和 `brief` 的 `variant`；`tests/test_hierarchical_engine.py` 覆盖楔形内部 3-3-3-3-3 和引导 5-3-5-3-5 许可。
2. WE-4：独立穷举覆盖 6/8/14 拐点，自动多窗比较已输出 `participants`，候选召回对照和 30/60 决策性能门槛通过。
3. WE-5：`compare_decisions()` 已提取并接入 `attach_previous`；评估网格共用轻量结构状态函数，条件重放单元测试覆盖。
4. 默认切换：本轮决定保留 `classic_v4` 为默认，`hierarchical_v1` 作为显式 opt-in；独立验收后可再评估默认切换。

独立验收清单见[独立验收清单](2026-11-20-wave-engine-independent-acceptance.md)。机器可读对照见 `reports/wave-engine/acceptance/comparison.json` 和 `reports/wave-engine/benchmark/comparison.json`；归档证据见[验收证据](2026-11-20-wave-engine-acceptance-evidence.json)。
