# F 标的涨跌标签与每模板二项概率模型记录

日期 2026-11-19。范围：F 的标的涨跌标签、每模板独立二项识别概率模型、case 均权、split 保留、E 式留出校准与发布门槛。A–E、G 见其他验收记录。

源码指纹 `0cfe453c5d02a245ffca148d76656ec337aa414551cf08b94350ece61d554342`；完整测试 254 项通过。

## 标的涨跌标签

### 标注者方向

- 盲标页面新增“标的涨跌”单选项：`UP / DOWN / ABSTAIN`。
- 标签 CSV 新增 `direction` 列；`import` 规范化为 `UP / DOWN / ABSTAIN`。
- `score` 新增 `directions`：
  - 方向计数
  - 双人有效 case 数
  - `up_up`、`down_down`、`discordant_cases`
  - 原始一致率
  - Cohen κ

### 实际未来方向

新增 `label_wave_cases.py direction`：

```bash
.venv/bin/python scripts/label_wave_cases.py direction \
  --cases blind-cases.json --cache-root .cache/eliotbot \
  --horizon 5 --output direction-labels.json
```

- 使用 case 的 `source_snapshot_id` 和 `decision_date`，读取决策后未来行情。
- 日线用 `future_daily_bars`，周线用 `future_weekly_bars`；个股按决策日复权坐标比较。
- 输出 `UP / DOWN / FLAT`，以及 `RIGHT_CENSORED`、`UNDEFINED`、`decision_close`、`future_close`、`return`、`horizon_date`。
- 实际方向标签单独存放，不进入盲标 case，避免未来信息泄漏。

真实烟测（茅台 `2026-02-27`，3 个 case，5 日）：

```text
case b78984e6e1e6b1c1b8fb26c2 DOWN 2026-03-06
  decision_close 1455.02, future_close 1402.0, return -3.64%
case c2cf2c08d8b0052ef47abac3 DOWN 2026-03-06
case e40572d8da78bff7177ca149 DOWN 2026-03-06
```

## 每模板独立二项模型

新增 `src/eliotbot/label_models.py`、`scripts/train_label_models.py` 和 `label_wave_cases.py predict`。

- 每模板一个独立二项逻辑回归；模板之间不共享系数。
- 目标：`case_target = 该 case 全部有效判读中认可该模板的比例`。
- 按 case 均权：每个 case 只贡献一个目标和一个样本权重；同一 case 的多份判读保留在同一 split。
- 若同一 case 的判读跨越多个 split，训练直接报错，不拆分判读。
- 特征：浪级、方向、歧义、资产类型、周期、case 类型、端点数量、K 线可用性。
- 训练/校准/留出：
  - 训练拟合二项逻辑回归。
  - 校准段做标量温度校准。
  - 留出段只评估一次。
- 基线：
  - 训练集频率（case 均权）。
  - 仅浪级特征的二项模型。
- 发布门槛：
  - F 标签门槛：训练集每模板至少 50 个双人有效 case、10 个双阳性、10 个双阴性、Cohen κ ≥ 0.60。
  - E 式留出门槛：测试至少 50 个 case、10 个日期、5 个标的；Brier/log loss 相对频率和浪级基线改善的配对 95% 区间下界 > 0；可靠性分档两侧各有支持档，预测均值减实际发生率 95% 区间在 ±0.10 内；支持档覆盖率 ≥ 0.5。
- 输出 `label-model.json`、`summary.json`、`manifest.json`；只有 `APPROVED` 模板可通过 `predict` 发布。

### 合成回归烟测

300 个 case：train 50（10 正/40 负）、calibration 50（10/40）、test 200（50/150）；正例由 `ambiguity=HIGH` 区分，浪级特征随机。

```text
model_id 3491695b676f...
IMPULSE status APPROVED
label_gate PASS（50 double / 10 dp / 40 dn / kappa 1.0）
test 200 cases / 20 dates / 20 symbols
supported_coverage 1.0
model Brier ~0.0000000004
frequency baseline Brier 0.1900
degree baseline Brier 0.1900
predictions positive ~0.99998，negative ~0.00002
```

该烟测验证审批路径、校准、留出指标和预测入口；真实人工标签尚未达到 50/10/10/κ 门槛，因此没有真实批准标签模型。

## 验证

```text
254 passed, 42 warnings
ruff check / format          PASS
pip check                    PASS
```

新增测试覆盖：

- 标注者方向导入、HTML 单选项、方向一致率/κ
- 实际未来方向标签（UP/DOWN/FLAT/截尾）
- case 均权目标
- 同一 case 跨 split 拒绝
- 每模板二项模型训练
- F 标签门槛与 E 式留出门槛
- 合成信号批准路径
- 样本不足拒绝路径
- 模型加载与预测

## 边界

- 实际涨跌标签是未来结果标签，不是盲标界面输入；盲标界面只收集标注者方向判断。
- 合成测试中的批准模型只用于验证管线，不是真实发布模型。
- 真实标签需要独立标注、达到 50/10/10/κ≥0.60 后，再按 E 式留出校准与门槛复验。
- 当前标签概率模型预测“独立判读认可概率”，不是标的涨跌概率；两者分别建标签。
- 所有改动未提交，等待验收。
