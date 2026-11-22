"""Audit missing invalidation/target geometry for the CN index target-first event."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from train_forecasts import _load_rows

from elliottwaver.config import Settings
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.errors import DataError
from elliottwaver.evaluation.decisions import read_evaluation_manifest
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.util import atomic_write, canonical_json

EVENT = "TARGET_FIRST_20D_V1"
SETTLED_STATUSES = {"TARGET_FIRST", "INVALIDATED_FIRST", "EXPIRED"}
UNSETTLED_STATUSES = {"INCOMPLETE_DATA", "RIGHT_CENSORED", "AMBIGUOUS_SAME_BAR"}

CATEGORY_STAGE_EXIT_ONLY = "STAGE_EXIT_ONLY"
CATEGORY_CONDITIONAL_NOT_TRIGGERED = "CONDITIONAL_INVALIDATION_NOT_TRIGGERED"
CATEGORY_SUSPECTED_MISSING = "SUSPECTED_MISSING_INVALIDATION"
CATEGORY_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
CATEGORY_INVALIDATION_PRESENT = "INVALIDATION_PRESENT_NO_TARGET"

CATEGORY_LABELS = {
    CATEGORY_STAGE_EXIT_ONLY: "只可给出阶段退出线的结构",
    CATEGORY_CONDITIONAL_NOT_TRIGGERED: "有硬规则但本轮样本未触发",
    CATEGORY_SUSPECTED_MISSING: "规则应给失效但实现可能漏发",
    CATEGORY_INSUFFICIENT: "特征缺失或数据证据不足",
    CATEGORY_INVALIDATION_PRESENT: "已有失效但缺目标区",
}


def _rule_assessment(stage: str, pattern: str, missing_invalidation: bool, scenario=None) -> dict:
    """Classify the projector branch that produced (or omitted) a boundary."""
    if not missing_invalidation:
        return {
            "category": CATEGORY_INVALIDATION_PRESENT,
            "branch": "INVALIDATION_PRESENT",
            "basis": "INVALIDATION present; UNDEFINED_EVENT because no feasible target zone",
        }
    if stage == "UNKNOWN" or pattern == "UNKNOWN":
        return {
            "category": CATEGORY_INSUFFICIENT,
            "branch": "UNKNOWN",
            "basis": "candidate features lack stage or pattern",
        }
    if stage == "COMPLETE":
        return {
            "category": CATEGORY_STAGE_EXIT_ONLY,
            "branch": "COMPLETE",
            "basis": "projector COMPLETE branch emits STAGE_EXIT only",
        }
    if pattern == "TRIANGLE":
        return {
            "category": CATEGORY_STAGE_EXIT_ONLY,
            "branch": "TRIANGLE",
            "basis": "projector TRIANGLE branch emits STAGE_EXIT (and confirmation) only",
        }
    if pattern == "FLAT" and stage == "B_DEVELOPING":
        return {
            "category": CATEGORY_STAGE_EXIT_ONLY,
            "branch": "FLAT/B",
            "basis": "projector FLAT/B branch emits STAGE_EXIT and confirmation only",
        }
    if pattern == "IMPULSE" and stage == "W5_DEVELOPING":
        return _impulse_w5_assessment(scenario)
    if pattern == "DIAGONAL" and stage == "W5_DEVELOPING":
        return {
            "category": CATEGORY_SUSPECTED_MISSING,
            "branch": "DIAGONAL/W5",
            "basis": "projector DIAGONAL/W5 should emit INVALIDATION at the wave-4 endpoint",
        }
    return {
        "category": CATEGORY_SUSPECTED_MISSING,
        "branch": f"{pattern}/{stage}",
        "basis": "hard-rule branch expected INVALIDATION; inspect candidate",
    }


def _impulse_w5_assessment(scenario) -> dict:
    prices = {}
    if scenario is not None and (scenario.pattern_type, scenario.stage) == (
        "IMPULSE",
        "W5_DEVELOPING",
    ):
        prices = {wave.label: wave.pivot.price for wave in scenario.completed_waves}
    if not {"0", "1", "2", "3", "4"} <= prices.keys():
        return {
            "category": CATEGORY_INSUFFICIENT,
            "branch": "IMPULSE/W5",
            "basis": "Exact candidate endpoints unavailable for IMPULSE/W5",
        }
    l1 = abs(prices["1"] - prices["0"])
    l3 = abs(prices["3"] - prices["2"])
    cap = prices["4"] + (l3 if scenario.pattern_direction == "UP" else -l3)
    if l3 >= l1:
        category = CATEGORY_CONDITIONAL_NOT_TRIGGERED
        basis = "Verified IMPULSE/W5: wave 3 is at least as long as wave 1"
    elif cap <= 0:
        category = CATEGORY_CONDITIONAL_NOT_TRIGGERED
        basis = "Verified IMPULSE/W5: invalidation price is outside the positive price domain"
    else:
        category = CATEGORY_SUSPECTED_MISSING
        basis = "Verified IMPULSE/W5: wave 3 is shorter than wave 1 and the cap is positive"
    return {
        "category": category,
        "branch": "IMPULSE/W5",
        "basis": basis,
        "wave_1_length": l1,
        "wave_3_length": l3,
        "cap_price": cap,
    }


def _chart(prepared, scenario, decision: date, title: str) -> str:
    bars = prepared.bars[-120:]
    if not bars:
        return ""
    width, height, pad = 760, 300, 24
    closes = [bar.close for bar in bars]
    low, high = min(closes), max(closes)
    levels = []
    if scenario is not None:
        levels = [
            (condition.kind, condition.price)
            for condition in scenario.conditions
            if condition.price > 0
        ]
        levels += [("TARGET", (zone.low + zone.high) / 2) for zone in scenario.target_zones]
        if levels:
            low = min(low, *(price for _, price in levels))
            high = max(high, *(price for _, price in levels))
    span = high - low or 1.0
    step = (width - 2 * pad) / max(1, len(bars) - 1)
    points = " ".join(
        f"{pad + index * step:.1f},{height - pad - (close - low) / span * (height - 2 * pad):.1f}"
        for index, close in enumerate(closes)
    )
    decision_index = next(
        (index for index, bar in enumerate(bars) if bar.session_date == decision),
        len(bars) - 1,
    )
    x = pad + decision_index * step
    colors = {
        "INVALIDATION": "#c0392b",
        "STAGE_EXIT": "#d68910",
        "CONFIRMATION": "#2471a3",
        "TARGET": "#1e8449",
    }
    lines = []
    for kind, price in levels[:10]:
        y = height - pad - (price - low) / span * (height - 2 * pad)
        color = colors.get(kind, "#7f8c8d")
        lines.append(
            f"<line x1='{pad}' y1='{y:.1f}' x2='{width - pad}' y2='{y:.1f}' "
            f"stroke='{color}' stroke-width='1' stroke-dasharray='5 3'/>"
            f"<text x='{width - pad - 4}' y='{y - 3:.1f}' font-size='10' "
            f"text-anchor='end' fill='{color}'>{kind} {price:.2f}</text>"
        )
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}'>"
        f"<rect width='{width}' height='{height}' fill='#fbfdff'/>"
        f"<polyline points='{points}' fill='none' stroke='#234' stroke-width='1.5'/>"
        f"<line x1='{x:.1f}' y1='{pad}' x2='{x:.1f}' y2='{height - pad}' "
        "stroke='#d1495b' stroke-width='2'/>"
        + "".join(lines)
        + f"<text x='{pad}' y='18' font-size='12' fill='#1b2b3a'>{title}</text></svg>"
    )


def _find_scenario(context, event: dict):
    candidate_key = event.get("candidate_key")
    for source, scenarios in (
        ("qualified", context.qualified),
        ("displayed", context.result.scenarios),
    ):
        scenario = next((item for item in scenarios if item.scenario_id == candidate_key), None)
        if scenario is not None:
            return scenario, source
    return None, None


def _representative(
    decision: dict,
    event: dict,
    assessment: dict,
    replay,
    chart_dir: Path,
    output_dir: Path,
) -> dict:
    features = event.get("features") or {}
    record = {
        "symbol": decision["symbol"],
        "decision_date": decision["decision_date"],
        "as_of": decision["as_of"],
        "source_snapshot_id": decision["source_snapshot_id"],
        "candidate_key": event.get("candidate_key"),
        "status": event.get("status"),
        "features": features,
        "rule_category": assessment["category"],
        "rule_category_label": CATEGORY_LABELS[assessment["category"]],
        "rule_branch": assessment["branch"],
        "rule_basis": assessment["basis"],
        "scenario_found": False,
        "scenario_source": None,
        "scenario_pattern": None,
        "scenario_stage": None,
        "conditions": [],
        "condition_kinds": {},
        "targets": [],
        "chart": None,
    }
    try:
        prepared, context = replay(decision)
        scenario, source = _find_scenario(context, event)
        if scenario is not None:
            record.update(
                {
                    "scenario_found": True,
                    "scenario_source": source,
                    "scenario_pattern": scenario.pattern_type,
                    "scenario_stage": scenario.stage,
                    "conditions": [item.model_dump(mode="json") for item in scenario.conditions],
                    "condition_kinds": dict(Counter(item.kind for item in scenario.conditions)),
                    "targets": [item.model_dump(mode="json") for item in scenario.target_zones],
                }
            )
        chart_dir.mkdir(parents=True, exist_ok=True)
        chart = chart_dir / f"{event.get('candidate_key', 'unknown')}.svg"
        chart.write_text(
            _chart(
                prepared,
                scenario,
                date.fromisoformat(decision["decision_date"]),
                f"{decision['symbol']} {decision['decision_date']} {assessment['branch']}",
            )
        )
        record["chart"] = str(chart.relative_to(output_dir))
    except Exception as exc:  # noqa: BLE001
        record["chart_error"] = f"{type(exc).__name__}: {exc}"
    return record


def audit(evaluation: Path, output_dir: Path) -> dict:
    manifest = read_evaluation_manifest(evaluation)
    protocol = manifest["protocol"]
    if EVENT not in protocol["events"]:
        raise ValueError("Evaluation does not contain the target-first event")
    decisions, events = _load_rows(evaluation)
    test_start = date.fromisoformat(protocol["splits"]["test"]["start"])
    cutoff = date.fromisoformat(protocol["grid"].get("outcome_as_of", protocol["grid"]["end"]))
    target = []
    for event in events:
        decision = decisions.get(event["decision_id"])
        if decision is None or event["event_id"] != EVENT:
            continue
        if decision.get("asset_type") != "INDEX" or decision.get("timeframe") != "1d":
            continue
        day = date.fromisoformat(decision["decision_date"])
        if day < test_start or not event.get("due_date"):
            continue
        due = date.fromisoformat(event["due_date"])
        if due > cutoff:
            continue
        target.append((decision, event))
    undefined = [item for item in target if item[1]["status"] == "UNDEFINED_EVENT"]
    missing_invalidation = [
        item
        for item in undefined
        if (item[1].get("features") or {}).get("missing_invalidation", 1.0) == 1
    ]
    with_invalidation = [
        item
        for item in undefined
        if (item[1].get("features") or {}).get("missing_invalidation") == 0
    ]
    service = AnalysisService(Settings(**protocol.get("settings", {})))
    store = SnapshotStore(Path(protocol.get("cache_root", ".cache/elliottwaver")))
    bundles = {}

    def replay(decision):
        snapshot_id = decision["source_snapshot_id"]
        if snapshot_id not in bundles:
            bundles[snapshot_id] = store.load(snapshot_id)
        request = AnalysisRequest(
            symbol=decision["symbol"],
            as_of=decision["as_of"],
            end=date.fromisoformat(decision["decision_date"]),
            mode=decision.get("mode", "FROZEN_DATA_REPLAY"),
            timeframe="1d",
            allow_gaps=bool(protocol.get("allow_gaps", False)),
        )
        prepared = prepare_bundle(request, bundles[snapshot_id])
        return prepared, service.analyze_with_context(request, prepared)

    conditional_by_decision = defaultdict(list)
    classes = defaultdict(list)
    for decision, event in missing_invalidation:
        features = event.get("features") or {}
        key = features.get("stage", "UNKNOWN"), features.get("pattern", "UNKNOWN")
        classes[key].append((decision, event))
        if key == ("W5_DEVELOPING", "IMPULSE"):
            conditional_by_decision[event["decision_id"]].append((decision, event))

    conditional_checks = []
    assessments = {}
    for decision_id, members in conditional_by_decision.items():
        error = None
        try:
            _, context = replay(members[0][0])
        except DataError as exc:
            context = None
            error = str(exc)
        for decision, event in members:
            scenario, source = (
                _find_scenario(context, event) if context is not None else (None, None)
            )
            assessment = _rule_assessment("W5_DEVELOPING", "IMPULSE", True, scenario)
            assessments[decision_id, event["candidate_key"]] = assessment
            conditional_checks.append(
                {
                    "decision_id": decision_id,
                    "symbol": decision["symbol"],
                    "decision_date": decision["decision_date"],
                    "source_snapshot_id": decision["source_snapshot_id"],
                    "candidate_key": event["candidate_key"],
                    "scenario_source": source,
                    "replay_error": error,
                    **assessment,
                }
            )

    def assess_event(key, event):
        if key == ("W5_DEVELOPING", "IMPULSE"):
            return assessments[event["decision_id"], event["candidate_key"]]
        return _rule_assessment(*key, True)

    breakdown = Counter()
    for key, members in classes.items():
        for _, event in members:
            assessment = assess_event(key, event)
            breakdown[
                (*key, assessment["category"], assessment["branch"], assessment["basis"])
            ] += 1
    categories = Counter()
    reasons = Counter()
    for (_, _, category, _, basis), count in breakdown.items():
        categories[category] += count
        reasons[basis] += count

    chart_dir = output_dir / "charts"
    representatives = []
    for key in (
        ("COMPLETE", "IMPULSE"),
        ("COMPLETE", "TRIANGLE"),
        ("B_DEVELOPING", "FLAT"),
        ("C_DEVELOPING", "TRIANGLE"),
        ("W5_DEVELOPING", "IMPULSE"),
    ):
        if classes.get(key):
            members = classes[key]
            decision, event = next(
                (
                    item
                    for item in members
                    if assess_event(key, item[1])["category"] == CATEGORY_SUSPECTED_MISSING
                ),
                members[0],
            )
            representatives.append(
                _representative(
                    decision, event, assess_event(key, event), replay, chart_dir, output_dir
                )
            )
    if with_invalidation:
        decision, event = with_invalidation[0]
        representatives.append(
            _representative(
                decision,
                event,
                _rule_assessment("UNKNOWN", "UNKNOWN", False),
                replay,
                chart_dir,
                output_dir,
            )
        )

    def has_both(event):
        features = event.get("features") or {}
        if features.get("missing_invalidation", 1.0) != 0:
            return False
        if "missing_target_geometry" in features:
            return features.get("missing_target_geometry", 1.0) == 0
        # The first-touch evaluator checks boundary presence before inspecting future bars.
        return event["status"] in SETTLED_STATUSES | UNSETTLED_STATUSES

    both_conditions = sum(has_both(event) for _, event in target)
    settled = sum(event["status"] in SETTLED_STATUSES for _, event in target)
    unresolved = Counter(
        event["status"]
        for _, event in target
        if event["status"] not in SETTLED_STATUSES and event["status"] != "UNDEFINED_EVENT"
    )
    stage_rows = [
        {
            "stage": stage,
            "pattern": pattern,
            "count": count,
            "share": count / len(missing_invalidation) if missing_invalidation else None,
            "rule_category": category,
            "rule_branch": branch,
            "rule_basis": basis,
            "evidence_quality": (
                "UNVERIFIED"
                if category == CATEGORY_INSUFFICIENT
                else "LOW_SAMPLE"
                if count < 10
                else "SUFFICIENT"
            ),
        }
        for (stage, pattern, category, branch, basis), count in breakdown.most_common()
    ]
    result = {
        "schema_version": "1.2",
        "event_id": EVENT,
        "test_start": str(test_start),
        "outcome_as_of": str(cutoff),
        "target_events": len(target),
        "undefined": len(undefined),
        "settled": settled,
        "unresolved": dict(unresolved),
        "missing_invalidation": len(missing_invalidation),
        "with_invalidation_missing_target": len(with_invalidation),
        "both_conditions": both_conditions,
        "both_conditions_rate": both_conditions / len(target) if target else None,
        "both_conditions_basis": (
            "V3: both boundary features present. V2: invalidation present and "
            "the first-touch evaluator passed its boundary checks, regardless of settlement."
        ),
        "reconciliation": {
            "undefined_equals_missing_invalidation_plus_with_invalidation": (
                len(undefined) == len(missing_invalidation) + len(with_invalidation)
            ),
            "missing_invalidation_equals_stage_pattern_sum": (
                len(missing_invalidation) == sum(breakdown.values())
            ),
            "target_events_equals_undefined_plus_settled_plus_unresolved": (
                len(target) == len(undefined) + settled + unresolved.total()
            ),
        },
        "rule_categories": [
            {
                "category": category,
                "label": CATEGORY_LABELS[category],
                "count": categories.get(category, 0),
                "share": (
                    categories.get(category, 0) / len(missing_invalidation)
                    if missing_invalidation
                    else None
                ),
            }
            for category in (
                CATEGORY_STAGE_EXIT_ONLY,
                CATEGORY_CONDITIONAL_NOT_TRIGGERED,
                CATEGORY_SUSPECTED_MISSING,
                CATEGORY_INSUFFICIENT,
            )
        ],
        "stage_pattern": stage_rows,
        "reasons": dict(reasons),
        "representatives": representatives,
        "conditional_checks": conditional_checks,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(output_dir / "audit.json", canonical_json(result) + "\n")
    lines = [
        "# 指数目标先达候选缺失效边界审计",
        "",
        f"- 事件：`{EVENT}`",
        f"- 测试开始：{test_start}，结果截止：{cutoff}",
        f"- 目标事件：{len(target)}",
        f"- 未定义：{len(undefined)}",
        f"- 已结算：{settled}，有条件但结果未判定：{unresolved.total()}",
        f"- 缺失效：{len(missing_invalidation)}",
        f"- 有失效但缺目标：{len(with_invalidation)}",
        f"- 同时具备目标与失效：{both_conditions}（{result['both_conditions_rate']:.1%}）"
        if result["both_conditions_rate"] is not None
        else "",
        f"- 对账：未定义 = 缺失效 + 有失效缺目标 → {len(undefined)} = "
        f"{len(missing_invalidation)} + {len(with_invalidation)}",
        f"- 口径：{result['both_conditions_basis']}",
        "",
        "## 规则分类",
        "",
        "| 分类 | 含义 | 数量 | 占缺失效 |",
        "| --- | --- | ---: | ---: |",
    ]
    lines += [
        f"| {row['category']} | {row['label']} | {row['count']} | "
        f"{format(row['share'], '.1%') if row['share'] is not None else '—'} |"
        for row in result["rule_categories"]
    ]
    lines += [
        "",
        f"逐候选复核了 {len(conditional_checks)} 条推动浪 5 记录。"
        f"疑似漏发 {categories[CATEGORY_SUSPECTED_MISSING]} 条，"
        f"证据不足 {categories[CATEGORY_INSUFFICIENT]} 条。"
        "各候选的浪长和上限见 `audit.json` 的 `conditional_checks`。",
        "",
        "## 阶段 × 模板",
        "",
        "| 阶段 | 模板 | 数量 | 占比 | 规则分类 | 证据 |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    lines += [
        f"| {row['stage']} | {row['pattern']} | {row['count']} | {row['share']:.1%} "
        f"| {row['rule_category']} | {row['evidence_quality']} |"
        for row in stage_rows
    ]
    lines += ["", "## 规则依据", ""]
    lines += [f"- {reason}: {count}" for reason, count in reasons.items()]
    lines += ["", "## 代表案例", ""]
    for row in representatives:
        kinds = ", ".join(
            f"{kind}={count}" for kind, count in sorted(row["condition_kinds"].items())
        )
        lines += [
            f"### {row['symbol']} {row['decision_date']} `{row['candidate_key']}`",
            f"- 快照：`{row['source_snapshot_id']}`",
            f"- 状态：{row['status']}",
            f"- 分类：{row['rule_category']}（{row['rule_category_label']}）",
            f"- 规则：{row['rule_basis']}",
            f"- 复算场景：{row['scenario_source'] or '未找到'}"
            f"（{row['scenario_pattern'] or '—'}/{row['scenario_stage'] or '—'}）",
            f"- 条件：{len(row['conditions'])}（{kinds or '无'}），目标：{len(row['targets'])}",
            f"- 图：{row['chart'] or '—'}",
            "",
        ]
    atomic_write(output_dir / "audit.md", "\n".join(lines) + "\n")
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.relative_to(output_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_write(
        output_dir / "manifest.json",
        canonical_json(
            {
                "schema_version": "1.0",
                "event_id": EVENT,
                "evaluation_manifest_sha256": hashlib.sha256(
                    (evaluation / "manifest.json").read_bytes()
                ).hexdigest(),
                "files": files,
            }
        )
        + "\n",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.evaluation, args.output_dir)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "target_events",
                    "undefined",
                    "settled",
                    "unresolved",
                    "missing_invalidation",
                    "with_invalidation_missing_target",
                    "both_conditions",
                )
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
