# 父级上下文与子浪认证独立验收

日期：2026-11-18。受验收提交：`0f566766d26dbb43a771cf38804143477226ad93`，对比基线：`867fe59`。开始时工作区干净。本轮只增加验收脚本、证据和记录，未修改引擎。

源码指纹：`6176f3f790cf8560a6657a3033dacafce0e16576e0440de3ba9514c0e39673f4`。计算方法是对排序后的 `src/**/*.py` 文件哈希映射计算仓库标准摘要。

## 结论

**新增认证能力暂不通过验收，存在两项已复现的 P2 问题。** 数据链路、报告摘要、历史修复和当前测试的回归检查通过；父级几何包含与子模板自身规则通过，仍不足以证明该子结构可以占据指定父浪位置。

| 检查 | 独立复验结果 |
| --- | --- |
| 完整 pytest | 171 项通过；42 条为已有第三方 timedelta 弃用告警 |
| Ruff lint / format、`pip check` | 通过 |
| 冻结真实快照 | 21 组重算，源码指纹、配置与 v3 证据一致，21/21 结果摘要一致；新发布的 JSON、Markdown、HTML 文件哈希和结果摘要通过校验 |
| 历史回放 | 10 个长历史前缀与同口径单点 API 摘要一致；公开 CLI 的缺口策略与显式深度 0 检查通过 |
| ENG-R01 至 ENG-R06 | 六项历史反例全部通过 |
| 新增反例 | P3-R01、P3-R02 各验证上涨及下跌镜像，均误标 `VERIFIED` |
| 正向对照 | 锯齿 C 腿匹配推动子结构、普通 5-3-5-3-5 子结构，正反方向共 4 组通过 |

结构化输出见 [独立验收证据](2026-11-18-parent-subwave-acceptance-evidence.json)，包含源码和探针指纹、反例、正向对照、真实样本端点，以及完整的历史复验摘要。

## P3-R01 · P2：几何匹配被当作父级位置认证

位置：[parents.py](../../src/eliotbot/engine/parents.py#L72)。实现检查浪级、时间包含和方向后直接产生 `EXACT_LEG/VERIFIED`，没有校验父模板该腿允许的子模板。当前文档确实将此状态定义为包含关系，因此这是新认证能力的设计缺口，不能用几何检查已经实现来认定父级位置已认证。

**合成反例：** 父锯齿端点 `[100,150,120,180]` 位于 `[0,100,200,300]`；子锯齿端点 `[120,145,130,180]` 位于 `[200,230,260,300]`。二者各自的全部硬规则通过，子结构完整占据父 C 腿。程序输出 `parent_pattern=ZIGZAG_ABC, parent_wave_label=C, parent_relation=EXACT_LEG, parent_context=VERIFIED`。但锯齿 C 应是推动类结构，不能由另一组三浪锯齿整体充当。下跌镜像同样复现。允许多层嵌套不能解释端点完全一致的一整条腿。

**真实样本：** `validation_20261118_cn_index_v3.json` 的 `szcomp_day`（深证成指日线）首个展示候选 `5834610ff7db5b795abe8d1a` 是完整 `IMPULSE`，却获得父三角形 `fbc1f1e2fe37fc06d73db4d2` 的 `C/EXACT_LEG/VERIFIED` 关系。两者对应的 C 腿起止都为 2025-11-24 至 2026-06-22；三角形该腿应是调整结构。父候选本身仍为 `D_DEVELOPING`，当前标记仅因 C 端点已确认就升为 `VERIFIED`，没有处理类型冲突。

**影响：** 直接影响 JSON、Markdown 和终端的父级认证标签；该字段当前不参与评分，不能声称它已经改变排名。它会把互斥计数表现成已认证的上下级关系。

**修复与验收条件：** 建立按父模板和浪段位置划分的子模板许可规则，并在父级关联时使用。至少拒绝上述完整精确腿冲突，同时保留正反向的合法推动 C 腿、进行中父腿和跨枢轴边界用例。若继续只提供几何包含信息，应将其明确标为几何关联，而非已认证的 Elliott 父级位置；`CONTAINED` 也应区分包含事实与直接子浪位置。

预期依据：[EWI 锯齿规则](https://www.elliottwave.com/waveopedia/zigzags/)的 5-3-5 结构，以及 [EWI 三角形规则](https://www.elliottwave.com/waveopedia/triangles/)的 3-3-3-3-3 结构。

## P3-R02 · P2：推动浪第 3 浪允许以楔形通过子浪认证

位置：[subwaves.py](../../src/eliotbot/engine/subwaves.py#L43) 和 [子模板选择](../../src/eliotbot/engine/subwaves.py#L93)。普通推动浪的第 1、3、5 腿都映射到 `MOTIVE`，再统一允许 `IMPULSE` 或 `DIAGONAL`。第 3 浪必须自身为推动浪，这个位置不能用楔形替代。单独运行楔形硬规则无法发现父位置不允许楔形的问题。

**反例：** 父推动端点 `[100,120,110,150,135,160]` 位于 `[0,10,20,30,40,50]`；第 1、5 腿内部设置普通推动，第 2、4 腿内部设置锯齿。第 3 腿用 `[100,130,115,141,128,149]` 的仿射变换填入 `[110,150]`，形成规则通过的收敛楔形。该腿若作为普通推动检查会触发 `W4_NO_OVERLAP`。

实际输出：第 `2->3` 腿 `pattern=DIAGONAL, status=VERIFIED`；整体 `status=VERIFIED, fit=1.0`，`subwave_structure` 得到全部 15 分权重的贡献。下跌镜像也一样。将同一腿换回合法普通推动后，正向对照仍通过。

**影响：** 错误子结构会得到完整子浪加分和可用证据状态，可能改变质量门槛、候选排名和相对权重；不能将 `VERIFIED` 理解为对应位置的 Elliott 规则已经全部通过。

**修复与验收条件：** 使用父模板及腿序号选择允许的子模板；普通推动第 3 腿仅允许 `IMPULSE`。同时区分需要不同内部结构的引导/终结楔形，或保留未认证状态，避免统一的 `MOTIVE/CORRECTIVE` 映射扩大认证范围。上述涨跌反例不得整体 `VERIFIED` 或获得满额子浪贡献；合法 5-3-5-3-5 对照必须继续通过。父级关联和子浪认证应共享位置规则，避免二者给出矛盾结论。

预期依据：[EWI 推动浪规则](https://www.elliottwave.com/waveopedia/impulse/)明确第三子浪必须为推动浪；仓库 `DESIGN.md` §9.4 也区分引导楔形（浪 1/A）和终结楔形（浪 5/C）。

## 复现与范围

纯合成反例不需要缓存、网络或密钥：

```bash
.venv/bin/python scripts/acceptance_parent_subwave_20261118.py
```

在本地冻结快照和 v3 报告仍存在时，复现完整独立验收：

```bash
.venv/bin/python scripts/acceptance_engine_20261118.py \
  --output reports/independent-p3-20261118/baseline-evidence.json \
  --reports reports/independent-p3-20261118/baseline
.venv/bin/python scripts/acceptance_parent_subwave_20261118.py \
  --frozen \
  --baseline reports/independent-p3-20261118/baseline-evidence.json \
  --output reports/independent-p3-20261118/acceptance-evidence.json
```

当前前一条命令退出 0，后一条退出 1；退出 1 表示两项预期未满足，证据已写出。修复后不能删除反例或直接修改预期为允许误认证，应使负向检查和正向对照同时通过。

本次未新增线上下载，也不评估收益、概率校准或真实时点数据完整性。深度 2 缺少下一层证据时保留深度 1 的行为已经在实现记录中明确，不作为新增缺陷；它也不能用来证明已经完成两层认证。父级浪级启发式、细拐点等距取样上限以及未支持的复杂结构仍按已有边界处理。
