# DeepSeek 三角形子模板许可独立验收

本次新增的三角形许可规则暂不通过验收。越界索引处理通过，上轮 P3-R01 和 P3-R02 均复验通过。

工作区基于 `2026-11-18 的“Add parent context and rule-based subwave certification”`。相对于上一轮已验证的源码指纹 `3ea00154e04b3272f98251a88dc6b53f885e5d1c249c6d9fb264960ad78be86a`，新增行为来自 `engine/templates.py`。本次源码指纹为 `bdd9820ba2c3d478c3b699841f16af4f319b274679ff41de6fbe36f734b169a5`。前后对比在独立临时目录恢复上一轮模板表并验证指纹。

## T-R01 · P2 平台可使三角形误获满额子浪分

新许可表将 `FLAT` 加入三角形全部五条腿。用 `[100, 180, 110, 166, 125, 154]` 构造收敛三角形，在每条腿中分别放入端点为 `[起价, 终价, 起价, 终价]` 的平台。五腿分别通过平台硬规则，均触犯锯齿的起点或 C 浪约束。上涨和下跌镜像均从整体 `UNVERIFIED`、0 分变成 `VERIFIED`、15 分。五腿均为锯齿的对照两版都通过。[许可表](../../src/eliotbot/engine/templates.py#L57)

该结果违反 Frost 与 Prechter 对收敛三角形的内部要求，五腿中至少四腿应是锯齿或锯齿组合。错误满分会进入质量和候选权重计算。修复验收应拒绝五平台反例的整体认证与满额加分，同时保留五锯齿对照。[《Elliott Wave Principle》收敛三角形规则](https://fliphtml5.com/xoxfq/mxyj/Elliott_Wave_Principle__Key_to_-_Robert_Prechter_%28267%29/)

## T-R02 · P3 一律拒绝嵌套三角形导致父级关系回归

许可表从 E 腿移除了 `TRIANGLE`，注释将这种嵌套定义为不成立。EWI 明确列出较少见的子三角形，常出现于 E 腿。独立探针让父三角形和填满 E 腿的子三角形分别通过仓库端点硬规则。相同端点下，两个方向的父级关系均从 `VERIFIED / EXACT_LEG / E` 退为 `UNKNOWN`。该反例验证位置许可这一层，依照当前引擎的端点模型构造。[EWI 三角形规则与九浪图例](https://www.elliottwave.com/waveopedia/triangles/)

应保留 E 腿嵌套的已支持关系，并继续约束子模板与端点规则。若产品选择缩小支持范围，应明确它是实现范围限制，同步变更引擎行为版本。两版目前共用 `subwave-v4` 标识，合成反例已显示不同关系和评分。

## 回归与复现

174 项 pytest、Ruff 检查与格式检查通过。六种模板的负索引及第一个越界索引均返回空。21 组冻结样本均重新生成报告并校验文件摘要，与上一轮源码的结果摘要逐组一致。10 个长历史回放前缀、ENG-R01 至 ENG-R06、深证成指父级反例及 P3-R01 和 P3-R02 的合成镜像全部通过。

以下命令在受验工作区退出 1，并写出含价格序列、涨跌镜像、源码指纹及各腿得分的 JSON。两项新反例失败，五锯齿正向对照通过。

```bash
.venv/bin/python scripts/acceptance_triangle_children_20261118.py \
  --output reports/deepseek-review-20261118/triangle-probe.json
```
