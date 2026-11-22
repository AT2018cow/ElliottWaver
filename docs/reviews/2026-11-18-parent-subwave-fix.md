# 父级位置与子浪认证修复

P3-R01 和 P3-R02 已通过复验。修复以 `2026-11-18 的“Add parent context and rule-based subwave certification”` 为基线，验证源码指纹为 `bdd9820ba2c3d478c3b699841f16af4f319b274679ff41de6fbe36f734b169a5`，引擎版本中的子浪标识升级到 `subwave-v4`。[初次验收](2026-11-18-parent-subwave-acceptance.md)

| 问题 | 修复后的行为 | 复验结果 |
| --- | --- | --- |
| P3-R01 父级误认证 | 精确匹配的子模板必须符合对应父腿位置。候选只满足时间包含时，状态为 `PROVISIONAL`。 | 涨跌镜像均拒绝锯齿填满另一锯齿的 C 腿，合法推动 C 腿均通过。深证成指原推动浪候选仍在报告中，与三角形 C 腿的关联被拒绝，改为匹配另一个推动浪候选的第 5 腿。 |
| P3-R02 第 3 浪误认证 | 推动浪第 3 腿只接受 `IMPULSE`，与父级关联共用位置规则。 | 涨跌镜像的第 3 腿和整体均为 `UNVERIFIED`。其余四腿通过，拟合为 `0.8`，子浪贡献为 `12/15`。合法 5-3-5-3-5 对照两向均获得满额贡献。 |
| 补充：三角形 E 腿许可 | 三角形五条腿统一允许 `ZIGZAG_ABC / FLAT / WXY`，拒绝五浪 `TRIANGLE` 充当单条 3 浪腿；`allowed_child_templates` 对越界腿序号返回空。 | 有效三角形成分填满 E 腿被拒绝，有效平台填满 E 腿通过；174 项测试。 |

深证成指日线快照为 `13b1fcba2dcaed829ea8afd0760d3d918d956018409abbe5a2eca62ed296febf`。两次分析均枚举到原子候选 `5834610ff7db5b795abe8d1a` 和原父候选 `fbc1f1e2fe37fc06d73db4d2`。修复后选中的父候选为 `bb15f509ce95ecffdef76a21`，新报告结果摘要为 `106b1ef9880e93c0b7ecaf5541731a3c802f61a7d3a18043ae290364079296a3`。

通用楔形端点模板尚未区分引导和终结类型，因此其内部子浪认证显示 `UNAVAILABLE`。允许楔形的位置仍可依据端点硬规则匹配楔形子模板。[楔形结构说明](https://www.elliottwave.com/waveopedia/elliott-wave-pattern-diagonals/)

## 复验

174 项 pytest 通过，42 条告警来自第三方 timedelta 弃用接口。Ruff 检查、格式检查和 `pip check` 通过。21 组已冻结样本全部重算，分析状态均与 v3 对齐。新报告的文件哈希和业务结果摘要通过校验。10 个长历史前缀的回放和单点分析摘要一致，ENG-R01 至 ENG-R06 六项反例继续通过。

纯合成复验可省略 `--frozen` 和 `--baseline`。完整复验使用同批本地快照和 v3 报告，JSON 输出在 `reports/parent-subwave-fix-20261118/`。

```bash
.venv/bin/python scripts/acceptance_engine_20261118.py \
  --output reports/parent-subwave-fix-20261118/regressions.json \
  --reports reports/parent-subwave-fix-20261118/frozen
.venv/bin/python scripts/acceptance_parent_subwave_20261118.py \
  --frozen \
  --baseline reports/parent-subwave-fix-20261118/regressions.json \
  --output reports/parent-subwave-fix-20261118/targeted.json
```
