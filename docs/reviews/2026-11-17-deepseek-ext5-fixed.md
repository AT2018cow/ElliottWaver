# DeepSeek ext5 验收问题修复与复验

日期：2026-11-17。基线为首次提交 `9c6db8f8b588636f014421f0882e5d05b162f419`；包含 DeepSeek 的第五浪与目标扩展，以及此次直接修复。

## 结论

**本轮修复复验通过。** R01、R02 均已修正；D01 通过显式区分新旧评分口径处理。100 项 pytest 测试、Ruff lint/format、同一批九组冻结真实快照重算及七项独立探针通过。验收范围仍是工程计算与产物一致性，未新增概率校准、收益验证或真实美股账户联调结论。

## 修复范围

| 编号 | 修复 | 验收 |
| --- | --- | --- |
| EXT5-R01 | 报告逐一列出真实有目标的候选，S2/S4 不再缩写成 S2-S4 | 新增四组回归：不连续、连续、单个、空集合；独立探针确认 `S2、S4` |
| EXT5-R02 | 更正文档，区分业务内容相同与结果身份相同 | 保留引擎参与摘要；新增回归验证修改引擎版本必然改变摘要；初版两组未变业务的历史结果身份变化仍得到验证 |
| EXT5-D01 | 默认配置改为 `ew_score_v2`；显式 `ew_score_v1` 使用原第五浪公式；服务按所选 profile 计算 | profile 参与配置摘要；未知值拒绝；保留旧 YAML 配置；对两种公式分别手算复核，并验证服务参数传递 |

v1 保留浪 5/浪 1 的原评分：核心 1.0/0.618、次优 1.618 权重 0.7、sigma=0.10。v2 保留 DeepSeek 的两套关系取较优拟合与扩展比例，不以恢复旧命名为理由修改新公式。其他评分特征共用，目标推演与报告使用当前引擎。

引擎标识增加 `report-v2`，使旧报告与修复后的产物使用不同结果身份及目录。该次修复的完整标识为：

```text
ew-rules-v1/zigzag-v1/score-v2/projection-v2/report-v2/0.1.0
```

P0/P1/P2 之后为 `ew-rules-v2/zigzag-v1/score-v2/projection-v3/report-v3/search-v2/0.1.0`；P3 后当前标识为 `ew-rules-v3/zigzag-v1/score-v2/subwave-v3/projection-v4/report-v4/search-v2/0.1.0`。

验证脚本也移除了固定写入“81 tests passed”和“没有凭证”的元数据。冻结快照脚本只报告自己做过的检查，pytest 和在线联调标记为 `NOT_RUN_BY_THIS_SCRIPT`，本文件单独记录实际测试结果。

重复验证的默认摘要写入忽略的 `reports/snapshot-validation.json`，避免直接覆盖历史证据；正式归档时再显式指定 `--output`。实现方案中的快照命令示例也已改为带引号的变量，避免占位符被 shell 当作重定向。

## 验证证据

- pytest：**100 passed**（本次验收时）；后续小调整、指数支持与 P0/P1/P2 后为 140，见下方说明。22 条原有第三方 timedelta 弃用告警。
- Ruff lint、format：通过。
- 独立评分：v2 的 90 组比例/尺度算例及 90 组涨跌镜像通过；新增 90 组 v1 独立公式复核通过。
- 完成态目标：16 组推动/ABC、涨跌镜像、深回撤与目标耗尽边界通过。
- 结构过滤：被排除候选不进入权重，空候选时未展示权重为 0。
- 真实数据：使用原有明确快照 ID，九组分析、逐前缀复核及 JSON/MD/HTML 报告均通过；不联网重抓数据。
- 身份绑定：修复后的源码和配置摘要与新验证记录匹配；每份结果摘要都按包含引擎版本的字段独立重算。

后续小调整（同日）：`ew_score_v1` 的第五浪解释不再展示浪 3 比值；结果 `engine_version` 末尾追加当前 profile（例如 `.../0.1.0/ew_score_v2`）。随后接入沪深京指数日线/周线；再完成 P0/P1/P2：长历史预设与日历裁剪、`--allow-gaps`、宽桥接/大级别搜索、端点级三角形/平台/楔形/WXY、`--subwave-depth` 存在性检查、浪级标签。pytest 更新为 **140 passed**，九组验证、指数验证、五组长历史验证与验收脚本均已重新生成并通过；本文件其余结论保持不变。

最新输入、结果身份、覆盖区间和源码指纹见[九组快照验证](../validation_20261117_ext5_fixed.json)。独立检查及逐文件指纹见[复验证据](2026-11-17-deepseek-ext5-fixed-evidence.json)。原始的[失败证据](2026-11-17-deepseek-ext5-evidence.json)与 [DeepSeek 初版验证](../validation_20261117_ext5.json)保持原样，作为历史记录，不冒充当前版本结果。

## 复现

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python scripts/acceptance_deepseek_ext5.py
```

独立探针默认读取 `docs/validation_20261117_ext5_fixed.json` 并写入 `2026-11-17-deepseek-ext5-fixed-evidence.json`；也可通过 `--validation`、`--output` 指定新产物路径。需要原有及修复后的报告目录、本地快照和基线 Git 对象。

本次提交范围为源代码、回归测试、配置样例、说明文档和验证摘要。凭证、虚拟环境、原始快照及生成报告仍由 `.gitignore` 排除。
