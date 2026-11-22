# `eliot brief` B 首版验收记录

日期 2026-11-19。范围是 B 组“周线背景与换窗复核”的首版：同一日线源派生周线、跨周期方向对齐、自动换窗敏感性。C–G 未纳入。

源码指纹 `00eb6914558f2cb5e5da17a1a6642043f1833d07ba2d07fba53d283c6540bc5c`，引擎 `ew-rules-v3/zigzag-v1/score-v2/subwave-v6/projection-v4/report-v4/search-v2/0.1.0`。

## 已实现

- `brief` 默认从同一日线源快照派生 `1w` 请求，保留相同 `as_of/end`，清空日线专属锚点。
- 周线结果、图表和条件使用自己的 `prepared` 输入；日线仍是正式主结果。
- `weekly_state = NOT_REQUESTED / AVAILABLE / INSUFFICIENT_DATA`。
- 跨周期状态：`ALIGNED / CONFLICT / UNDETERMINED / NOT_AVAILABLE`。
- 自动换窗：主分析至少两个自动锚点时，以最晚锚点为替代搜索下界重跑；状态为 `SAME_COUNT / CHANGED_COUNT_SAME_BIAS / CHANGED_BIAS / INCONCLUSIVE / NOT_APPLICABLE`。
- 换窗只作敏感性说明，不覆盖正式日线主选。
- `--no-weekly`、`--no-window-check` 可关闭。
- 发布 `weekly.json` 和 `weekly.html`（周线可用时）。

## 验收

```text
188 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
```

真实快照烟测 `600519.SH` 日线源（`8378a32d...`）：

```text
weekly_state: AVAILABLE
weekly.status: UNCLEAR
weekly.input_snapshot_id: 9dd41d72...
cross_timeframe: UNDETERMINED（周线下一段 UNKNOWN）
window_comparison: SAME_COUNT（替代起点 2024-10-08，360 根）
```

生成文件：

```text
brief.json
daily.json
weekly.json
report.md
index.html
daily.html
weekly.html
manifest.json
```

合成演示行情（周线历史不足）：

```text
weekly_state: INSUFFICIENT_DATA
cross_timeframe: NOT_AVAILABLE
window_comparison: CHANGED_COUNT_SAME_BIAS
```

日线摘要仍可打开，周线不足不阻断主结果。

## 复现

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/eliot brief 600519.SH --offline \
  --snapshot 8378a32d2ce12a8419cb853ad4c6558aa4644f11aae7302c3319d14530d87f8d \
  --output-dir reports/brief-b --format html,md
```

## 边界

- 跨周期对齐只比较下一段方向，不推断父子浪认证。
- 周线背景只在主周期为日线时生成；主周期为周线时不重复生成。
- 换窗复核只做一次，不替代正式结果。
- 历史查询、自选列表、概率模型和盲标仍未实现。
- 本次未提交，等待验收。
