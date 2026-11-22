"""Compare fixed and rolling calibration for the CN index target-first event."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from prepare_index_target import CADENCE, EVENT, FOLDS
from train_forecasts import (
    MOMENTUM_FEATURES,
    _features,
    _fit_model,
    _load_rows,
    _price_order,
    _release,
    _split_rows,
    forecast_evidence,
)

from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.evaluation.calibration import rolling_logit_offset
from elliottwaver.evaluation.decisions import read_evaluation_manifest
from elliottwaver.evaluation.features import TARGET_FEATURES, event_feature_order
from elliottwaver.util import atomic_write, canonical_json, digest

START = date(2023, 1, 1)
DEVELOPMENT_END = date(2025, 12, 31)
CLASSES = ["NO", "YES"]
GROUPS = {
    "A": {"features": "V2", "calibration": "fixed", "feature_version": "2", "rolling": False},
    "B": {"features": "V2", "calibration": "rolling", "feature_version": "2", "rolling": True},
    "C": {"features": "V3", "calibration": "fixed", "feature_version": "3", "rolling": False},
    "D": {"features": "V3", "calibration": "rolling", "feature_version": "3", "rolling": True},
}
ROLLING_KEYS = (
    "rolling_window_dates",
    "rolling_min_dates",
    "rolling_min_rows",
    "rolling_min_positive",
    "rolling_min_negative",
    "rolling_l2",
)


def _sigmoid(values):
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50, 50)))


def _probability_matrix(values):
    positive = _sigmoid(values)
    return np.column_stack([1.0 - positive, positive])


def _matrix(rows, order):
    return np.asarray([_features(row, row["decision"], order) for row in rows], dtype=float)


def _base_logits(rows, parameters, order):
    x = (_matrix(rows, order) - np.asarray(parameters["mean"])) / np.asarray(parameters["scale"])
    weights = np.asarray(parameters["weights"])
    intercept = np.asarray(parameters["intercept"])
    return (x @ (weights[:, 1] - weights[:, 0]) + intercept[1] - intercept[0]) / parameters.get(
        "temperature", 1.0
    )


def _serialize_parameters(parameters: dict) -> dict:
    if "probability" in parameters:
        return {"probability": parameters["probability"]}
    return {
        "mean": parameters["mean"].tolist(),
        "scale": parameters["scale"].tolist(),
        "weights": parameters["weights"].tolist(),
        "intercept": parameters["intercept"].tolist(),
        "temperature": parameters["temperature"],
    }


def _fit_baseline(train, calibration, test, order):
    probabilities, parameters = _fit_model(
        _matrix(train, order),
        np.asarray([row["y"] for row in train], dtype=int),
        _matrix(calibration, order),
        np.asarray([row["y"] for row in calibration], dtype=int),
        _matrix(test, order),
        2,
        calibration_method="binary_logit_affine",
    )
    return (
        probabilities,
        _base_logits(calibration, parameters, order),
        _base_logits(test, parameters, order),
        parameters,
    )


def _frequency_baseline(train, calibration, test):
    rate = float(np.mean([row["y"] for row in train]))
    logit = float(np.log(rate / (1.0 - rate)))
    probabilities = np.tile([1.0 - rate, rate], (len(test), 1))
    return (
        probabilities,
        np.full(len(calibration), logit),
        np.full(len(test), logit),
        {"probability": rate},
    )


def _rolling_states(calibration, test, calibration_logits, test_logits, release):
    history = []
    for rows, logits in ((calibration, calibration_logits), (test, test_logits)):
        for row, logit in zip(rows, logits, strict=True):
            history.append(
                {
                    "decision_date": row["decision_date"].isoformat(),
                    "available": row.get("label_available_at"),
                    "z": float(logit),
                    "y": int(row["y"]),
                }
            )
    states = {}
    by_date = defaultdict(list)
    for row in test:
        by_date[row["decision_date"]].append(row)
    for day in sorted(by_date):
        rows = by_date[day]
        as_of = rows[0]["as_of"]
        if any(row["as_of"] != as_of for row in rows):
            raise ValueError(f"decision as_of differs within {day}")
        states[day] = rolling_logit_offset(
            [item["z"] for item in history],
            [item["y"] for item in history],
            [item["decision_date"] for item in history],
            [item["available"] for item in history],
            as_of,
            window_dates=release["rolling_window_dates"],
            min_dates=release["rolling_min_dates"],
            min_rows=release["rolling_min_rows"],
            min_positive=release["rolling_min_positive"],
            min_negative=release["rolling_min_negative"],
            l2=release["rolling_l2"],
        )
    return states


def _fixed_states(test):
    return {
        row["decision_date"]: {
            "delta": 0.0,
            "rows": 0,
            "dates": 0,
            "positive": 0,
            "negative": 0,
            "last_label_available_at": None,
            "window_start": None,
            "window_end": None,
            "reason": "fixed calibration",
        }
        for row in test
    }


def _fallback(states):
    return sum(1 for state in states.values() if state["reason"] not in {None, "fixed calibration"})


def _label_available(decision, event, due, due_times, store):
    snapshot_id = decision["source_snapshot_id"]
    if snapshot_id not in due_times:
        due_times[snapshot_id] = {
            bar.session_date: bar.usable_at for bar in store.load(snapshot_id).bars
        }
    usable_at = due_times[snapshot_id].get(due)
    claimed = event.get("label_available_at")
    if claimed is not None and datetime.fromisoformat(claimed) != usable_at:
        raise ValueError(f"Label availability does not match the frozen due bar for {snapshot_id}")
    if usable_at is None:
        return None
    if usable_at <= datetime.fromisoformat(decision["as_of"]):
        raise ValueError(f"Due bar is already available at the decision for {snapshot_id}")
    return usable_at.isoformat()


def _load_target_rows(directory, manifest):
    protocol = manifest["protocol"]
    if protocol["grid"]["cadence"] != CADENCE or EVENT not in protocol["events"]:
        raise ValueError("Incompatible target-first evaluation")
    decisions, events = _load_rows(directory)
    symbols = {
        sample["symbol"] for sample in protocol["samples"] if sample["asset_type"] == "INDEX"
    }
    snapshots = {
        sample["symbol"]: sample["snapshot_id"]
        for sample in protocol["samples"]
        if sample["symbol"] in symbols
    }
    store = SnapshotStore(Path(protocol.get("cache_root", ".cache/elliottwaver")))
    due_times = {}
    rows = {}
    all_events = {}
    for event in events:
        decision = decisions.get(event["decision_id"])
        if decision is None or event["event_id"] != EVENT:
            continue
        day = date.fromisoformat(decision["decision_date"])
        if decision["symbol"] not in symbols or day < START or day > DEVELOPMENT_END:
            continue
        if decision["asset_type"] != "INDEX" or decision["timeframe"] != "1d":
            continue
        if not event.get("due_date"):
            continue
        due = date.fromisoformat(event["due_date"])
        if due > DEVELOPMENT_END:
            continue
        key = (decision["symbol"], day, event["candidate_key"])
        if key in all_events:
            raise ValueError(f"Duplicate target candidate for {key}")
        if due <= day or decision["source_snapshot_id"] != snapshots[decision["symbol"]]:
            raise ValueError(f"Invalid due date or snapshot for {key}")
        all_events[key] = {
            "decision_id": event["decision_id"],
            "decision_date": day,
            "due_date": due,
            "symbol": decision["symbol"],
            "status": event["status"],
            "source_snapshot_id": decision["source_snapshot_id"],
        }
        label = {"TARGET_FIRST": 1, "INVALIDATED_FIRST": 0, "EXPIRED": 0}.get(event["status"])
        if label is None or not event.get("features") or not decision.get("features"):
            continue
        available = _label_available(decision, event, due, due_times, store)
        rows[key] = {
            "key": key,
            "decision_id": event["decision_id"],
            "decision_date": day,
            "due_date": due,
            "symbol": decision["symbol"],
            "as_of": decision["as_of"],
            "source_snapshot_id": decision["source_snapshot_id"],
            "decision": decision,
            "features": event["features"],
            "y": label,
            "label_available_at": available,
        }
    return rows, all_events


def _fold_windows(ends):
    train_end, calibration_end, test_end = (date.fromisoformat(day) for day in ends)
    return {
        "train": (START, train_end),
        "calibration": (train_end + timedelta(days=1), calibration_end),
        "test": (calibration_end + timedelta(days=1), test_end),
    }


def _pooled_evidence(rows, probabilities, baselines, release):
    return forecast_evidence(EVENT, rows, CLASSES, probabilities, baselines, release, CADENCE)


def _reliability_table(evidence):
    return [
        {
            "bin": f"{row['low']:.0%}-{row['high']:.0%}",
            "count": row["count"],
            "mean_prediction": row["mean_confidence"],
            "observed_rate": row["accuracy"],
        }
        for row in evidence["metrics"]["reliability"]
    ]


def _increment_label(row):
    if row["beats_price_baseline_gate"]:
        return "有增量"
    if (
        row["price_baseline_brier_improvement"] < 0
        and row["price_baseline_log_loss_improvement"] < 0
    ):
        return "负增益"
    return "未建立"


def _report(result, output_dir):
    metadata = result["metadata"]
    metrics = result["metrics"]
    selection = result["selection"]
    lines = [
        "# 指数 20 日目标先达：固定与滚动校准对照",
        "",
        "## 复现",
        "",
        "```bash",
        metadata["command"],
        "```",
        "",
        "范围固定为 `TARGET_FIRST_20D_V1`、7 个沪深京指数、`sina-index` 无复权日线和每周最后一个交易日。"
        "选择只使用 2023–2025 开发期；2026 留出仅作诊断，不参与选型。",
        "",
        "## 输入与对齐",
        "",
        f"- V2 评估：`{metadata['v2']['directory']}`，feature_version={metadata['v2']['feature_version']}",
        f"- V3 评估：`{metadata['v3']['directory']}`，feature_version={metadata['v3']['feature_version']}",
        f"- 对齐候选：{metadata['alignment']['candidates']} 条可判定事件；"
        f"全部事件 {metadata['alignment']['all_events']} 条",
        "- 对齐键：`(symbol, decision_date, candidate_key)`；候选集合、标签、决策日期、"
        "预定到期日和状态逐项一致。",
        f"- `decision_id` 因协议身份进入摘要而按版本不同："
        f"{metadata['alignment']['decision_id_mismatches']} 条不同，未作为跨版本键。",
        "",
        "| 折 | 训练末日 | 校准末日 | 验证末日 |",
        "| --- | --- | --- | --- |",
    ]
    for index, (train_end, calibration_end, test_end) in enumerate(FOLDS, 1):
        lines.append(f"| {index} | {train_end} | {calibration_end} | {test_end} |")
    lines += [
        "",
        "| 组 | 特征 | 校准 |",
        "| --- | --- | --- |",
        "| A | V2 | 固定带截距校准 |",
        "| B | V2 | 固定校准 + 滚动截距 |",
        "| C | V3（目标几何） | 固定带截距校准 |",
        "| D | V3（目标几何） | 固定校准 + 滚动截距 |",
        "",
        "## 合并指标",
        "",
        "| 组 | 行数 | Brier | Log loss | 支持档覆盖率 | 状态 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for group in "ABCD":
        pooled = metrics["groups"][group]["pooled"]
        lines.append(
            f"| {group} | {pooled['test_rows']} | {pooled['metrics']['brier']:.4f} "
            f"| {pooled['metrics']['log_loss']:.4f} | {pooled['supported_coverage']:.1%} "
            f"| {pooled['status']} |"
        )
    lines += [
        "",
        "## 逐折指标",
        "",
        "| 组 | 折 | 行数 | Brier | Log loss | 支持档覆盖率 | 可结算覆盖率 | 回退日期 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in "ABCD":
        for fold in metrics["groups"][group]["folds"]:
            evidence = fold["evidence"]
            lines.append(
                f"| {group} | {fold['fold']} | {fold['rows']} "
                f"| {evidence['metrics']['brier']:.4f} "
                f"| {evidence['metrics']['log_loss']:.4f} "
                f"| {evidence['supported_coverage']:.1%} "
                f"| {fold['settleable_coverage']:.1%} "
                f"| {fold['fallback_dates']}/{len(fold['states'])} |"
            )
    lines += [
        "",
        "## 配对改善（合并，相对固定基线）",
        "",
        "| 组 | 基线 | Brier 改善 | Brier 95% 区间 | Log loss 改善 | Log loss 95% 区间 |",
        "| --- | --- | ---: | --- | ---: | --- |",
    ]
    for group in "ABCD":
        improvements = metrics["groups"][group]["pooled"]["improvement"]
        for name in ("frequency", "momentum", "price"):
            row = improvements[name]
            brier_ci = row["brier_ci"]
            loss_ci = row["log_loss_ci"]
            lines.append(
                f"| {group} | {name} | {row['brier']:+.4f} "
                f"| [{brier_ci[0]:+.4f}, {brier_ci[1]:+.4f}] "
                f"| {row['log_loss']:+.4f} "
                f"| [{loss_ci[0]:+.4f}, {loss_ci[1]:+.4f}] |"
            )
    lines += [
        "",
        "滚动组另报告对基线应用同样滚动截距的对照；这些结果在 `metrics.json` 的 "
        "`improvement` 中以 `*_rolling` 键保存。",
        "",
        "## 选型",
        "",
        "- 参照：A（V2 + 固定校准）",
        f"- 入选：{selection['selected']}",
        f"- 理由：{selection['reason']}",
        f"- 满足两项合并损失均不高于 A 且至少一项严格改善的组："
        f"{', '.join(selection['eligible']) or '无'}",
        "",
        "| 组 | Brier | Log loss | 相对 A 的 Brier | 相对 A 的 Log loss |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    reference = selection["pooled"]["A"]
    for group in "ABCD":
        pooled = selection["pooled"][group]
        lines.append(
            f"| {group} | {pooled['brier']:.4f} | {pooled['log_loss']:.4f} "
            f"| {pooled['brier'] - reference['brier']:+.4f} "
            f"| {pooled['log_loss'] - reference['log_loss']:+.4f} |"
        )
    lines += [
        "",
        "开发期状态只表示折内门槛，不是发布批准；A 在开发期显示 `APPROVED`，"
        "但 2026 留出验收仍是校准未通过。",
        "",
        "## 波浪结构的增量",
        "",
        "V3 的模型与价格基线使用同一目标几何、标签和日期。相对价格基线的正改善表示"
        "波浪特征仍有增量；负改善表示目标几何已足以解释收益。",
        "",
        "| 组 | 价格基线 Brier 改善 | Brier 95% 区间 | 价格基线 Log loss 改善 | Log loss 95% 区间 | 增量结论 |",
        "| --- | ---: | --- | ---: | --- | --- |",
    ]
    for group in "ABCD":
        row = selection["diagnostics"][group]
        lines.append(
            f"| {group} | {row['price_baseline_brier_improvement']:+.4f} "
            f"| [{row['price_baseline_brier_ci'][0]:+.4f}, {row['price_baseline_brier_ci'][1]:+.4f}] "
            f"| {row['price_baseline_log_loss_improvement']:+.4f} "
            f"| [{row['price_baseline_log_loss_ci'][0]:+.4f}, {row['price_baseline_log_loss_ci'][1]:+.4f}] "
            f"| {_increment_label(row)} |"
        )
    lines += [
        "",
        f"- 选型结论：{selection['recommendation']}",
        "",
        f"候选状态：`{result['frozen']['stage']}`；"
        f"`{result['frozen']['holdout_status']}`。本轮没有新增独立前向留出，"
        "因此不注册正式模型，也不把开发期选择写成发布证据。",
        "",
        "## 选中组的分档可靠性",
        "",
        "| 预测档 | 样本数 | 平均预测 | 实际发生率 |",
        "| --- | ---: | ---: | ---: |",
    ]
    selected = selection["selected"]
    for row in _reliability_table(metrics["groups"][selected]["pooled"]):
        lines.append(
            f"| {row['bin']} | {row['count']} | {row['mean_prediction']:.1%} "
            f"| {row['observed_rate']:.1%} |"
        )
    lines += [
        "",
        "## 失效边界审计",
        "",
        "上级目录的 `audit-v2/audit.md` 复算 1,548 个未定义、1,534 个缺失效和 14 个有失效缺目标；"
        "规则分类与代表案例、SVG 图见该文件。",
        "",
        "## 文件校验",
        "",
        "`manifest.json` 保存本目录除自身外所有文件的 SHA-256；"
        "`metadata.json` 保存 V2/V3 清单、协议和源码指纹。",
        "",
    ]
    atomic_write(output_dir / "report.md", "\n".join(lines) + "\n")


def compare(v2_evaluation: Path, v3_evaluation: Path, output_dir: Path, command: str) -> dict:
    v2_manifest = read_evaluation_manifest(v2_evaluation)
    v3_manifest = read_evaluation_manifest(v3_evaluation)
    if (v2_manifest["feature_version"], v3_manifest["feature_version"]) != ("2", "3"):
        raise ValueError("Comparison requires feature version 2 in V2 and version 3 in V3")
    for field in ("engine_version", "config_hash"):
        if v2_manifest[field] != v3_manifest[field]:
            raise ValueError(f"V2/V3 {field} mismatch")
    v2_rows, v2_all = _load_target_rows(v2_evaluation, v2_manifest)
    v3_rows, v3_all = _load_target_rows(v3_evaluation, v3_manifest)
    if set(v2_rows) != set(v3_rows) or set(v2_all) != set(v3_all):
        raise ValueError("V2 and V3 target candidate sets differ")
    for key in v2_all:
        left, right = v2_all[key], v3_all[key]
        for field in ("decision_date", "due_date", "symbol", "status", "source_snapshot_id"):
            if left[field] != right[field]:
                raise ValueError(f"V2/V3 all-event {field} mismatch for {key}")
    for key in v2_rows:
        left, right = v2_rows[key], v3_rows[key]
        for field in ("as_of", "y", "label_available_at"):
            if left[field] != right[field]:
                raise ValueError(f"V2/V3 {field} mismatch for {key}")
        shared = {
            name: value for name, value in right["features"].items() if name not in TARGET_FEATURES
        }
        if (
            left["features"] != shared
            or left["decision"]["features"] != right["decision"]["features"]
        ):
            raise ValueError(f"V2/V3 common features differ for {key}")
    decision_id_mismatches = sum(
        1 for key in v2_rows if v2_rows[key]["decision_id"] != v3_rows[key]["decision_id"]
    )
    v2_release = _release(v2_manifest["protocol"])
    v3_release = _release(v3_manifest["protocol"])
    if v2_release != v3_release:
        raise ValueError("V2/V3 release settings differ")
    release = dict(v2_release)
    release.update(
        {
            "rolling_window_dates": 12,
            "rolling_min_dates": 8,
            "rolling_min_rows": 100,
            "rolling_min_positive": 10,
            "rolling_min_negative": 10,
            "rolling_l2": 0.1,
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_by_group = {"A": v2_rows, "B": v2_rows, "C": v3_rows, "D": v3_rows}
    metrics = {
        "schema_version": "1.0",
        "event_id": EVENT,
        "folds": FOLDS,
        "groups": {},
        "alignment": {
            "key": "(symbol, decision_date, candidate_key)",
            "decision_id_note": (
                "decision_id is version-local because the protocol identity is part of its digest; "
                "the stable key omits it while checking candidate, label, due date and status"
            ),
            "candidates": len(v2_rows),
            "all_events": len(v2_all),
            "v2_events": len(v2_all),
            "v3_events": len(v3_all),
            "decision_id_mismatches": decision_id_mismatches,
            "labels": {
                "positive": sum(row["y"] for row in v2_rows.values()),
                "negative": sum(1 - row["y"] for row in v2_rows.values()),
            },
            "mismatches": 0,
        },
    }
    fold_parameters = {}
    for group, definition in GROUPS.items():
        rows = rows_by_group[group]
        feature_version = definition["feature_version"]
        rolling = definition["rolling"]
        order = event_feature_order(EVENT, feature_version)
        group_folds = []
        pooled_rows = []
        pooled_prob = []
        pooled_baselines = defaultdict(list)
        group_dir = output_dir / "groups" / group
        group_dir.mkdir(parents=True, exist_ok=True)
        for fold_index, ends in enumerate(FOLDS, 1):
            windows = _fold_windows(ends)
            split = _split_rows(list(rows.values()), windows)
            if not split["test"]:
                raise ValueError(f"Fold {fold_index} {group} has no test rows")
            model_prob, parameters = _fit_model(
                _matrix(split["train"], order),
                np.asarray([row["y"] for row in split["train"]], dtype=int),
                _matrix(split["calibration"], order),
                np.asarray([row["y"] for row in split["calibration"]], dtype=int),
                _matrix(split["test"], order),
                2,
                calibration_method="binary_logit_affine",
            )
            calibration_logits = _base_logits(split["calibration"], parameters, order)
            test_logits = _base_logits(split["test"], parameters, order)
            states = (
                _rolling_states(
                    split["calibration"], split["test"], calibration_logits, test_logits, release
                )
                if rolling
                else _fixed_states(split["test"])
            )
            delta = np.asarray([states[row["decision_date"]]["delta"] for row in split["test"]])
            if rolling:
                model_prob = _probability_matrix(test_logits + delta)
            baseline_prob = {}
            baseline_cal_logits = {}
            baseline_test_logits = {}
            frequency_prob, frequency_cal, frequency_test, frequency_parameters = (
                _frequency_baseline(split["train"], split["calibration"], split["test"])
            )
            baseline_prob["frequency"] = frequency_prob
            baseline_cal_logits["frequency"] = frequency_cal
            baseline_test_logits["frequency"] = frequency_test
            for name, baseline_order in (
                ("momentum", MOMENTUM_FEATURES),
                ("price", _price_order(EVENT, feature_version)),
            ):
                prob, cal_logits, test_logits_b, parameters_b = _fit_baseline(
                    split["train"], split["calibration"], split["test"], baseline_order
                )
                baseline_prob[name] = prob
                baseline_cal_logits[name] = cal_logits
                baseline_test_logits[name] = test_logits_b
                fold_parameters.setdefault(group, {}).setdefault(f"fold-{fold_index}", {})[name] = (
                    _serialize_parameters(parameters_b)
                )
            baselines = dict(baseline_prob)
            baseline_states = {}
            if rolling:
                for name in ("frequency", "momentum", "price"):
                    baseline_states[name] = _rolling_states(
                        split["calibration"],
                        split["test"],
                        baseline_cal_logits[name],
                        baseline_test_logits[name],
                        release,
                    )
                    baseline_delta = np.asarray(
                        [
                            baseline_states[name][row["decision_date"]]["delta"]
                            for row in split["test"]
                        ]
                    )
                    baselines[f"{name}_rolling"] = _probability_matrix(
                        baseline_test_logits[name] + baseline_delta
                    )
            evidence = _pooled_evidence(split["test"], model_prob, baselines, release)
            all_test = {
                key
                for key, item in v2_all.items()
                if item["decision_date"] >= windows["test"][0]
                and item["decision_date"] <= windows["test"][1]
                and item["due_date"] <= windows["test"][1]
            }
            test_keys = {row["key"] for row in split["test"]}
            fallback = _fallback(states)
            record = {
                "fold": fold_index,
                "group": group,
                "feature_version": feature_version,
                "features": definition["features"],
                "calibration": definition["calibration"],
                "rolling": rolling,
                "order": order,
                "rows": len(split["test"]),
                "states": {str(day): state for day, state in states.items()},
                "settleable_coverage": len(test_keys) / len(all_test) if all_test else None,
                "fallback_dates": fallback,
                "fallback_rate": fallback / len(states) if states else 0.0,
                "baseline_fallback_rates": {
                    name: _fallback(value) / len(value) if value else 0.0
                    for name, value in baseline_states.items()
                },
                "baseline_orders": {
                    "frequency": ["frequency"],
                    "momentum": MOMENTUM_FEATURES,
                    "price": _price_order(EVENT, feature_version),
                },
                "evidence": evidence,
                "prediction_file": f"groups/{group}/fold-{fold_index}-predictions.jsonl",
                "calibration_file": f"groups/{group}/fold-{fold_index}-calibration.json",
            }
            group_folds.append(record)
            metrics["groups"].setdefault(group, {"folds": [], "pooled": None})["folds"].append(
                record
            )
            pooled_rows.extend(split["test"])
            pooled_prob.append(model_prob)
            for name, values in baselines.items():
                pooled_baselines[name].append(values)
            with (group_dir / f"fold-{fold_index}-predictions.jsonl").open("w") as stream:
                for index, (row, probability) in enumerate(
                    zip(split["test"], model_prob, strict=True)
                ):
                    output = {
                        "group": group,
                        "fold": fold_index,
                        "decision_id": row["decision_id"],
                        "alignment_key": [
                            row["symbol"],
                            row["decision_date"].isoformat(),
                            row["key"][2],
                        ],
                        "decision_date": row["decision_date"].isoformat(),
                        "as_of": row["as_of"],
                        "symbol": row["symbol"],
                        "candidate_key": row["key"][2],
                        "due_date": row["due_date"].isoformat(),
                        "label_available_at": row["label_available_at"],
                        "y": row["y"],
                        "p_model": float(probability[1]),
                        "z": float(test_logits[index]),
                        "delta": float(delta[index]),
                        "fallback_reason": states[row["decision_date"]]["reason"],
                    }
                    for name, values in baselines.items():
                        output[f"p_{name}"] = float(values[index][1])
                    stream.write(canonical_json(output) + "\n")
            with (group_dir / f"fold-{fold_index}-calibration.json").open("w") as stream:
                stream.write(
                    canonical_json(
                        {
                            "fold": fold_index,
                            "group": group,
                            "states": {str(day): state for day, state in states.items()},
                            "calibration_inputs": [
                                {
                                    "decision_date": row["decision_date"].isoformat(),
                                    "symbol": row["symbol"],
                                    "candidate_key": row["key"][2],
                                    "y": row["y"],
                                    "label_available_at": row["label_available_at"],
                                    "z": float(calibration_logits[index]),
                                }
                                for index, row in enumerate(split["calibration"])
                            ],
                            "test_inputs": [
                                {
                                    "decision_date": row["decision_date"].isoformat(),
                                    "symbol": row["symbol"],
                                    "candidate_key": row["key"][2],
                                    "y": row["y"],
                                    "label_available_at": row["label_available_at"],
                                    "z": float(test_logits[index]),
                                }
                                for index, row in enumerate(split["test"])
                            ],
                            "baseline_states": {
                                name: {str(day): state for day, state in value.items()}
                                for name, value in baseline_states.items()
                            },
                        }
                    )
                    + "\n"
                )
            fold_parameters[group].setdefault(f"fold-{fold_index}", {})["model"] = (
                _serialize_parameters(parameters)
            )
        pooled = _pooled_evidence(
            pooled_rows,
            np.vstack(pooled_prob),
            {name: np.vstack(values) for name, values in pooled_baselines.items()},
            release,
        )
        metrics["groups"][group]["pooled"] = pooled
    baseline_group = metrics["groups"]["A"]["pooled"]["metrics"]
    eligible = []
    for group in ("B", "C", "D"):
        pooled = metrics["groups"][group]["pooled"]["metrics"]
        if (
            pooled["brier"] <= baseline_group["brier"]
            and pooled["log_loss"] <= baseline_group["log_loss"]
            and (
                pooled["brier"] < baseline_group["brier"]
                or pooled["log_loss"] < baseline_group["log_loss"]
            )
        ):
            eligible.append(group)
    priority = {"A": 0, "B": 1, "C": 2, "D": 3}
    if eligible:
        selected = min(
            eligible,
            key=lambda group: (
                metrics["groups"][group]["pooled"]["metrics"]["log_loss"],
                priority[group],
            ),
        )
        reason = "eligible; lowest pooled log loss; ties prefer fewer changes"
    else:
        selected = "A"
        reason = "no candidate improved both pooled losses without worsening one; retain A"
    diagnostics = {}
    for group in "ABCD":
        price = metrics["groups"][group]["pooled"]["improvement"]["price"]
        brier_ci = price["brier_ci"]
        loss_ci = price["log_loss_ci"]
        diagnostics[group] = {
            "price_baseline_brier_improvement": price["brier"],
            "price_baseline_brier_ci": brier_ci,
            "price_baseline_log_loss_improvement": price["log_loss"],
            "price_baseline_log_loss_ci": loss_ci,
            "beats_price_baseline_point": price["brier"] > 0 and price["log_loss"] > 0,
            "beats_price_baseline_gate": (
                brier_ci is not None and loss_ci is not None and brier_ci[0] > 0 and loss_ci[0] > 0
            ),
        }
    if selected in {"C", "D"} and not diagnostics[selected]["beats_price_baseline_gate"]:
        recommendation = (
            f"{selected} 相对 A 有改善，但未能在两项合并损失上以正的 95% 下界超过"
            "含目标几何的价格基线；波浪结构的增量未建立。保持研究状态，"
            "注册前必须等待新的独立留出。"
        )
    elif selected in {"C", "D"}:
        recommendation = f"{selected} 同时超过 A 和含目标几何的价格基线；冻结后等待独立留出再注册。"
    else:
        recommendation = (
            f"{selected} 是改动最少的回退；B/C/D 均未在两项合并损失上超过 A。归档负结果并保留 A。"
        )
    selection = {
        "reference": "A",
        "eligible": eligible,
        "selected": selected,
        "reason": reason,
        "status_note": (
            "development-fold status is not a release approval; the 2026 holdout remains "
            "the external calibration check"
        ),
        "diagnostics": diagnostics,
        "recommendation": recommendation,
        "pooled": {
            group: {
                "brier": metrics["groups"][group]["pooled"]["metrics"]["brier"],
                "log_loss": metrics["groups"][group]["pooled"]["metrics"]["log_loss"],
            }
            for group in "ABCD"
        },
    }
    metadata = {
        "schema_version": "1.0",
        "command": command,
        "event_id": EVENT,
        "cadence": CADENCE,
        "folds": FOLDS,
        "release": release,
        "groups": GROUPS,
        "v2": {
            "directory": str(v2_evaluation),
            "manifest_sha256": hashlib.sha256(
                (v2_evaluation / "manifest.json").read_bytes()
            ).hexdigest(),
            "protocol_sha256": v2_manifest["protocol_sha256"],
            "source_sha256": v2_manifest["source_sha256"],
            "feature_version": v2_manifest["feature_version"],
        },
        "v3": {
            "directory": str(v3_evaluation),
            "manifest_sha256": hashlib.sha256(
                (v3_evaluation / "manifest.json").read_bytes()
            ).hexdigest(),
            "protocol_sha256": v3_manifest["protocol_sha256"],
            "source_sha256": v3_manifest["source_sha256"],
            "feature_version": v3_manifest["feature_version"],
        },
        "alignment": metrics["alignment"],
    }
    frozen = {
        "schema_version": "1.0",
        "stage": "RESEARCH_COMPLETE_AWAITING_HOLDOUT",
        "event_id": EVENT,
        "selected_group": selected,
        "selected_definition": GROUPS[selected],
        "engine_version": v3_manifest["engine_version"],
        "config_hash": v3_manifest["config_hash"],
        "scope": {
            "markets": ["CN"],
            "asset_types": ["INDEX"],
            "timeframes": ["1d"],
            "feed": ["sina-index"],
            "adjustment": ["none"],
        },
        "symbols": sorted(
            sample["symbol"]
            for sample in v3_manifest["protocol"]["samples"]
            if sample["asset_type"] == "INDEX"
        ),
        "grid": v3_manifest["protocol"]["grid"],
        "allow_gaps": bool(v3_manifest["protocol"].get("allow_gaps", False)),
        "feature_version": GROUPS[selected]["feature_version"],
        "rolling": GROUPS[selected]["rolling"],
        "feature_order": event_feature_order(EVENT, GROUPS[selected]["feature_version"]),
        "baseline_orders": {
            "frequency": ["frequency"],
            "momentum": MOMENTUM_FEATURES,
            "price": _price_order(EVENT, GROUPS[selected]["feature_version"]),
        },
        "rolling_parameters": {key: release[key] for key in ROLLING_KEYS},
        "release": release,
        "folds": FOLDS,
        "fold_parameters": fold_parameters.get(selected, {}),
        "final_parameters": None,
        "final_parameters_note": (
            "not fit because no independent holdout has been scheduled; "
            "fold_parameters preserve the selected development models"
        ),
        "selected_metrics": {
            "pooled": metrics["groups"][selected]["pooled"],
            "folds": metrics["groups"][selected]["folds"],
        },
        "selection": selection,
        "diagnostics": diagnostics,
        "recommendation": recommendation,
        "holdout_requirements": {
            "min_rows": release["min_test_rows"],
            "min_dates": release["min_test_dates"],
            "min_symbols": release["min_test_symbols"],
            "min_supported_coverage": release["min_supported_coverage"],
            "min_bin_count": release["min_bin_count"],
            "min_bin_dates": release["min_bin_dates"],
            "reliability_tolerance": release["reliability_tolerance"],
        },
        "v2_protocol_sha256": metadata["v2"]["protocol_sha256"],
        "v3_protocol_sha256": metadata["v3"]["protocol_sha256"],
        "v2_source_sha256": metadata["v2"]["source_sha256"],
        "v3_source_sha256": metadata["v3"]["source_sha256"],
        "frozen_at": None,
        "freeze_status": "NOT_FROZEN_UNTIL_INDEPENDENT_HOLDOUT_START",
        "holdout_status": "AWAITING_NEW_INDEPENDENT_HOLDOUT",
        "candidate_id": None,
    }
    frozen["candidate_id"] = digest(
        {key: value for key, value in frozen.items() if key != "candidate_id"}
    )
    drill_protocol = {
        "schema_version": "1.0",
        "protocol_id": "cn-index-target-accuracy-drill-2023-2025",
        "event_id": EVENT,
        "cadence": CADENCE,
        "scope": {
            "markets": ["CN"],
            "asset_types": ["INDEX"],
            "timeframes": ["1d"],
            "feed": ["sina-index"],
            "adjustment": ["none"],
        },
        "samples": v3_manifest["protocol"]["samples"],
        "grid": v3_manifest["protocol"]["grid"],
        "folds": FOLDS,
        "groups": GROUPS,
        "rolling_parameters": {key: release[key] for key in ROLLING_KEYS},
        "release": release,
        "v2_protocol_sha256": metadata["v2"]["protocol_sha256"],
        "v3_protocol_sha256": metadata["v3"]["protocol_sha256"],
    }
    for name, value in (
        ("drill-protocol.json", drill_protocol),
        ("metadata.json", metadata),
        ("metrics.json", metrics),
        ("selection.json", selection),
        ("frozen-candidate.json", frozen),
    ):
        atomic_write(output_dir / name, canonical_json(value) + "\n")
    _report(
        {"metadata": metadata, "metrics": metrics, "selection": selection, "frozen": frozen},
        output_dir,
    )
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.relative_to(output_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "event_id": EVENT,
        "selected_group": selected,
        "v2_manifest_sha256": metadata["v2"]["manifest_sha256"],
        "v3_manifest_sha256": metadata["v3"]["manifest_sha256"],
        "files": files,
    }
    atomic_write(output_dir / "manifest.json", canonical_json(manifest) + "\n")
    return {
        "metadata": metadata,
        "metrics": metrics,
        "selection": selection,
        "frozen": frozen,
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2-evaluation", type=Path, required=True)
    parser.add_argument("--v3-evaluation", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    command = (
        "python scripts/compare_index_target.py "
        f"--v2-evaluation {args.v2_evaluation} "
        f"--v3-evaluation {args.v3_evaluation} "
        f"--output-dir {args.output_dir}"
    )
    result = compare(args.v2_evaluation, args.v3_evaluation, args.output_dir, command)
    print(
        json.dumps(
            {
                "selection": result["selection"],
                "metadata": {
                    "alignment": result["metadata"]["alignment"],
                    "v2": result["metadata"]["v2"],
                    "v3": result["metadata"]["v3"],
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
