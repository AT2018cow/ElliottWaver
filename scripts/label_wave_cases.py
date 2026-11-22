"""Export blind wave-label cases and score independent annotations."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from elliottwaver.data.periods import as_weekly
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.errors import DataError
from elliottwaver.evaluation.outcomes import outcome_window
from elliottwaver.label_models import (
    load_label_model,
    predict_label_probabilities,
    validate_label_identities,
)
from elliottwaver.schemas.market import asset_type_for
from elliottwaver.util import atomic_write, canonical_json, digest
from elliottwaver.visualization.brief_formatter import read_brief_report

HIDDEN_KEYS = {
    "future_bars",
    "model_choice",
    "weights",
    "preferred_pattern",
    "probability",
    "pattern_type",
    "stage",
    "quality_score",
    "coverage",
    "rejection_reason",
    "name",
    "role",
}
MIN_DOUBLE_CASES = 50
MIN_DOUBLE_POSITIVE = 10
MIN_DOUBLE_NEGATIVE = 10
MIN_KAPPA = 0.60
LABEL_KIND = "INDEPENDENT_RECOGNITION"
DEFAULT_BLIND_PROTOCOL = "1.0"


def export_cases(input_path: Path, output_path: Path) -> dict:
    payload = json.loads(input_path.read_text())
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("cases must be a JSON list")
    output = []
    for case in cases:
        for key in ("case_id", "symbol", "timeframe", "decision_date", "degree", "endpoints"):
            if key not in case:
                raise ValueError(f"case is missing {key}")
        clean = {key: value for key, value in case.items() if key not in HIDDEN_KEYS}
        clean["endpoints"] = [
            {**endpoint, "label": str(index)} if isinstance(endpoint, dict) else endpoint
            for index, endpoint in enumerate(case["endpoints"], 1)
        ]
        output.append(clean)
    result = {
        "schema_version": "1.1",
        "blind_protocol_version": DEFAULT_BLIND_PROTOCOL,
        "cases": output,
    }
    atomic_write(output_path, canonical_json(result) + "\n")
    return result


def _bars_for_brief(brief: dict, cache_root: Path, window: int) -> tuple[list[dict], bool]:
    from elliottwaver.schemas.market import AnalysisRequest
    from elliottwaver.service import prepare_bundle

    store = SnapshotStore(cache_root)
    try:
        source = store.load(brief["source_snapshot_id"])
        prepared = prepare_bundle(
            AnalysisRequest(
                symbol=brief["symbol"],
                as_of=brief["as_of"],
                end=brief["market_asof"],
                timeframe=brief["timeframe"],
                mode=brief.get("mode", "FROZEN_DATA_REPLAY"),
            ),
            source,
        )
        if prepared.snapshot_id != brief["input_snapshot_id"]:
            prepared = store.load(brief["input_snapshot_id"])
        if prepared.provenance.price_coordinate_id != brief["price_coordinate_id"]:
            return [], False
        bars = prepared.bars[-window:]
    except (DataError, OSError, KeyError, ValueError):
        return [], False
    return (
        [
            {
                "date": bar.session_date.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ],
        bool(bars),
    )


def _ambiguity(brief: dict, card: dict, cards: list[dict]) -> str:
    score = 0
    if brief.get("status") == "UNCLEAR":
        score += 2
    cross = (brief.get("cross_timeframe") or {}).get("state")
    if cross == "CONFLICT":
        score += 2
    elif cross == "UNDETERMINED":
        score += 1
    window = (brief.get("window_comparison") or {}).get("state")
    if window == "CHANGED_BIAS":
        score += 2
    elif window == "CHANGED_COUNT_SAME_BIAS":
        score += 1
    if len(cards) > 1:
        score += 1
    if float(brief.get("omitted_weight") or 0) > 0:
        score += 1
    if card.get("role") == "ALTERNATE":
        score += 1
    return "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"


def _no_candidate_case(
    brief: dict, asset_type: str, bars: list[dict], bars_available: bool
) -> dict:
    status = brief.get("status", "UNKNOWN")
    case_id = digest(
        [brief["symbol"], brief["timeframe"], brief["market_asof"], "NO_CANDIDATE", status]
    )[:24]
    warnings = ["NO_QUALIFIED_CANDIDATE"] if status == "UNCLEAR" else [status]
    if not bars_available:
        warnings.append("BARS_UNAVAILABLE")
    return {
        "case_id": case_id,
        "case_type": "NO_CANDIDATE",
        "symbol": brief["symbol"],
        "timeframe": brief["timeframe"],
        "decision_date": brief["market_asof"],
        "source_snapshot_id": brief.get("source_snapshot_id"),
        "asset_type": asset_type,
        "status": status,
        "degree": "UNKNOWN",
        "direction": "UNKNOWN",
        "ambiguity": "HIGH",
        "stratum": f"{asset_type}|UNKNOWN|UNKNOWN|HIGH",
        "endpoints": [],
        "endpoints_complete": True,
        "bars": bars,
        "bars_available": bars_available,
        "warnings": warnings,
    }


_EXCLUDED = re.compile(
    r"^(?P<pattern>[A-Z_]+)/(?P<stage>\S+) q=(?P<quality>[0-9.]+) "
    r"cov=(?P<coverage>[0-9.]+) reason=(?P<reason>[^;]+); "
    r"(?P<span>[^ ]+) (?P<points>.+)$"
)


def _excluded_case(
    brief: dict,
    asset_type: str,
    message: str,
    bars: list[dict],
    bars_available: bool,
) -> dict | None:
    match = _EXCLUDED.match(message)
    if match is None:
        return None
    points = [
        float(price)
        for price in re.findall(r"(?:^|\s)[0-9A-Z]+=([0-9]+(?:\.[0-9]+)?)", match.group("points"))
    ]
    if len(points) < 2:
        return None
    span = match.group("span").split("→")
    if len(span) != 2:
        return None
    endpoints = []
    for index, price in enumerate(points):
        if index == 0:
            kind = "UNKNOWN"
            day = span[0]
        elif index == len(points) - 1:
            kind = "UNKNOWN"
            day = span[1]
        elif price > points[index - 1] and price > points[index + 1]:
            kind = "HIGH"
            day = None
        elif price < points[index - 1] and price < points[index + 1]:
            kind = "LOW"
            day = None
        else:
            kind = "UNKNOWN"
            day = None
        endpoints.append(
            {
                "label": str(index + 1),
                "kind": kind,
                "date": day,
                "price": price,
                "bar_id": None,
            }
        )
    reason = match.group("reason").strip()
    ambiguity = "HIGH" if reason == "quality" else "MEDIUM"
    case_id = digest(
        [
            brief["symbol"],
            brief["timeframe"],
            brief["market_asof"],
            "EXCLUDED_STRUCTURE",
            match.group("pattern"),
            match.group("stage"),
            match.group("span"),
            match.group("points"),
        ]
    )[:24]
    warnings = ["ENDPOINT_DATES_PARTIAL"]
    if not bars_available:
        warnings.append("BARS_UNAVAILABLE")
    return {
        "case_id": case_id,
        "case_type": "EXCLUDED_STRUCTURE",
        "symbol": brief["symbol"],
        "timeframe": brief["timeframe"],
        "decision_date": brief["market_asof"],
        "source_snapshot_id": brief.get("source_snapshot_id"),
        "asset_type": asset_type,
        "status": "REJECTED",
        "degree": "UNKNOWN",
        "direction": "UNKNOWN",
        "ambiguity": ambiguity,
        "stratum": f"{asset_type}|UNKNOWN|UNKNOWN|{ambiguity}",
        "endpoints": endpoints,
        "endpoints_complete": False,
        "bars": bars,
        "bars_available": bars_available,
        "warnings": warnings,
    }


def _case_from_card(
    brief: dict, daily: dict, card: dict, bars: list[dict], bars_available: bool
) -> dict | None:
    scenario = next(
        (item for item in daily["scenarios"] if item["scenario_id"] == card["scenario_id"]),
        None,
    )
    if scenario is None:
        return None
    endpoints = [
        {
            "label": str(index),
            "kind": wave["pivot"]["kind"],
            "date": wave["pivot"]["extreme_at"][:10],
            "price": wave["pivot"]["price"],
            "bar_id": wave["pivot"]["extreme_bar_id"],
        }
        for index, wave in enumerate(scenario["completed_waves"], 1)
    ]
    asset_type = asset_type_for(
        daily["instrument"]["market"], daily["instrument"]["canonical_symbol"]
    )
    case_id = digest(
        [
            brief["symbol"],
            brief["timeframe"],
            brief["market_asof"],
            card["degree"],
            card["pattern_direction"],
            endpoints,
        ]
    )[:24]
    ambiguity = _ambiguity(brief, card, brief.get("cards", []))
    case = {
        "case_id": case_id,
        "case_type": "CANDIDATE",
        "symbol": brief["symbol"],
        "timeframe": brief["timeframe"],
        "decision_date": brief["market_asof"],
        "source_snapshot_id": brief.get("source_snapshot_id"),
        "asset_type": asset_type,
        "status": brief.get("status", "UNKNOWN"),
        "degree": card["degree"],
        "direction": card["pattern_direction"],
        "ambiguity": ambiguity,
        "stratum": (f"{asset_type}|{card['degree']}|{card['pattern_direction']}|{ambiguity}"),
        "endpoints": endpoints,
        "endpoints_complete": True,
        "bars": bars,
        "bars_available": bars_available,
        "warnings": [] if bars_available else ["BARS_UNAVAILABLE"],
    }
    tip = scenario.get("developing_tip")
    if tip is not None:
        case["developing_tip"] = {
            "kind": tip["kind"],
            "date": tip["extreme_at"][:10],
            "price": tip["price"],
            "bar_id": tip["extreme_bar_id"],
        }
    return case


PATTERNS = ["IMPULSE", "ZIGZAG_ABC", "TRIANGLE", "FLAT", "DIAGONAL", "WXY", "OTHER"]


def _svg(case: dict) -> str:
    bars = case.get("bars", [])
    if not bars:
        return "<p class='muted'>K 线不可用</p>"
    width, height, pad = 760, 220, 20
    closes = [bar["close"] for bar in bars]
    low, high = min(closes), max(closes)
    span = high - low or 1.0
    step = (width - 2 * pad) / max(1, len(bars) - 1)
    points = " ".join(
        f"{pad + index * step:.1f},{height - pad - (close - low) / span * (height - 2 * pad):.1f}"
        for index, close in enumerate(closes)
    )
    dates = {bar["date"]: index for index, bar in enumerate(bars)}
    markers = []
    for number, endpoint in enumerate(case.get("endpoints", []), 1):
        index = dates.get(endpoint.get("date"))
        if index is None:
            continue
        x = pad + index * step
        y = height - pad - (endpoint["price"] - low) / span * (height - 2 * pad)
        color = (
            "#d1495b"
            if endpoint["kind"] == "HIGH"
            else "#00798c"
            if endpoint["kind"] == "LOW"
            else "#6b7280"
        )
        day = endpoint.get("date") or "日期未知"
        markers.append(
            f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}'>"
            f"<title>端点 {number} {html.escape(day)} "
            f"{endpoint['price']:.2f}</title></circle>"
        )
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='K线缩略图'>"
        f"<rect width='{width}' height='{height}' fill='#fbfdff'/>"
        f"<polyline points='{points}' fill='none' stroke='#234' stroke-width='1.5'/>"
        f"{''.join(markers)}</svg>"
    )


def blind_html(cases: list[dict], title: str = "ElliottWaver 盲标") -> str:
    sections = []
    for case_number, case in enumerate(cases, 1):
        endpoints = (
            "".join(
                f"<li>端点 {number} · {html.escape(item['kind'])} · "
                f"{html.escape(item.get('date') or '日期未知')} · {item['price']:.4f}</li>"
                for number, item in enumerate(case.get("endpoints", []), 1)
            )
            or "<li>无端点（无候选）</li>"
        )
        checks = "".join(
            f"<label><input type='checkbox' name='pattern' value='{pattern}'> {pattern}</label>"
            for pattern in PATTERNS
        )
        sections.append(
            f"<section class='case' data-case-id='{html.escape(case['case_id'])}'>"
            f"<h2>{html.escape(case['symbol'])} · {html.escape(case['timeframe'])} · "
            f"{html.escape(case['decision_date'])}</h2>"
            f"<p>类型 {html.escape(case.get('case_type', 'CANDIDATE'))} · "
            f"浪级 {html.escape(case.get('degree', 'UNKNOWN'))} · "
            f"方向 {html.escape(case.get('direction', 'UNKNOWN'))} · "
            f"歧义 {html.escape(case.get('ambiguity', 'UNKNOWN'))} · "
            f"分层 {html.escape(case['stratum'])}</p>"
            f"{_svg(case)}"
            f"<h3>端点</h3><ol>{endpoints}</ol>"
            "<h3>标的涨跌</h3><p>"
            f"<label><input type='radio' name='direction-{case_number}' data-direction value='UP'> 上涨</label> "
            f"<label><input type='radio' name='direction-{case_number}' data-direction value='DOWN'> 下跌</label> "
            f"<label><input type='radio' name='direction-{case_number}' data-direction value='ABSTAIN'> 无法判定</label>"
            "</p>"
            f"<h3>选择可能的浪型（可多选）</h3><div class='patterns'>{checks}</div>"
            "<p><label><input type='checkbox' name='abstain'> 无法判定 / 弃权</label></p>"
            "<p><label>理由 <input name='reason' type='text' size='60'></label></p>"
            "<p><label><input type='checkbox' name='reviewed'> 已完成本案例判断</label></p>"
            "</section>"
        )
    return f"""<!doctype html>
<html lang="zh"><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f4f7fa;color:#1b2b3a}}
main{{max-width:1000px;margin:24px auto;padding:0 18px}}
.case{{background:#fff;border:1px solid #d8e2ea;border-radius:10px;padding:18px;margin:18px 0}}
svg{{width:100%;height:auto;border:1px solid #e3ebf1;border-radius:6px}}
.patterns{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px}}
.muted{{color:#5a6b7b}} .bar{{position:sticky;top:0;background:#12324a;color:#fff;padding:12px;border-radius:8px}}
button{{padding:8px 14px;font-size:15px}}
</style>
<main><div class="bar"><b>{html.escape(title)}</b>
<label>标注者 <input id="annotator" placeholder="annotator_id"></label>
<button onclick="downloadCsv()">导出 labels.csv</button>
<span class="muted">盲标协议 {DEFAULT_BLIND_PROTOCOL}。不展示模型选择、权重或未来行情。</span></div>
<p class="muted">共 {len(cases)} 个 case。请独立判断，可多选或弃权。导出只包含勾选「已完成本案例判断」的案例。</p>
{"".join(sections)}
</main>
<script>
function csv(value){{ return '"' + String(value ?? '').replaceAll('"','""') + '"'; }}
function downloadCsv() {{
  const annotator = document.getElementById('annotator').value.trim();
  if (!annotator) {{ alert('请填写 annotator_id'); return; }}
  const rows = [['case_id','annotator_id','selected_patterns','direction','abstain','reason']];
  document.querySelectorAll('.case').forEach(section => {{
    if (!section.querySelector("input[name='reviewed']").checked) return;
    const patterns = [...section.querySelectorAll("input[name='pattern']:checked")].map(x => x.value);
    const direction = section.querySelector("input[data-direction]:checked")?.value || 'ABSTAIN';
    const abstain = section.querySelector("input[name='abstain']").checked;
    const reason = section.querySelector("input[name='reason']").value;
    rows.push([section.dataset.caseId, annotator, patterns.join('|'), direction, abstain ? 'true' : 'false', reason]);
  }});
  const text = rows.map(row => row.map(csv).join(',')).join('\\n') + '\\n';
  const blob = new Blob([text], {{type:'text/csv;charset=utf-8'}});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'labels.csv';
  link.click();
}}
</script></html>"""


def export_brief_cases(
    briefs_root: Path,
    output_path: Path,
    *,
    cache_root: Path = Path(".cache/elliottwaver"),
    bar_window: int = 120,
    max_per_stratum: int | None = None,
    html_path: Path | None = None,
) -> dict:
    if bar_window < 20:
        raise ValueError("bar_window must be at least 20")
    cases: dict[str, dict] = {}
    strata: Counter[str] = Counter()
    source_briefs = []
    warnings = []
    seen_briefs = set()
    for brief_path in sorted(briefs_root.rglob("brief.json")):
        if any(part.startswith(".brief-") for part in brief_path.parts):
            warnings.append(f"{brief_path}: unpublished staging directory")
            continue
        try:
            verified, analysis = read_brief_report(brief_path.parent)
        except DataError as exc:
            warnings.append(f"{brief_path}: {exc}")
            continue
        if verified.brief_digest in seen_briefs:
            continue
        seen_briefs.add(verified.brief_digest)
        brief = verified.model_dump(mode="json")
        daily = analysis.model_dump(mode="json")
        bars, bars_available = _bars_for_brief(brief, cache_root, bar_window)
        source_briefs.append(
            {
                "brief_digest": brief["brief_digest"],
                "symbol": brief["symbol"],
                "timeframe": brief["timeframe"],
                "market_asof": brief["market_asof"],
            }
        )
        asset_type = asset_type_for(
            daily["instrument"]["market"], daily["instrument"]["canonical_symbol"]
        )
        cards = brief.get("cards", [])
        if not cards:
            case = _no_candidate_case(brief, asset_type, bars, bars_available)
            if case["case_id"] not in cases:
                cases[case["case_id"]] = case
                strata[case["stratum"]] += 1
        for card in cards:
            case = _case_from_card(brief, daily, card, bars, bars_available)
            if case is None:
                warnings.append(f"{brief_path}: card scenario missing from daily.json")
                continue
            if case["case_id"] not in cases:
                cases[case["case_id"]] = case
                strata[case["stratum"]] += 1
        for issue in brief.get("issues", []):
            if issue.get("code") != "EXCLUDED_STRUCTURE":
                continue
            case = _excluded_case(brief, asset_type, issue.get("message", ""), bars, bars_available)
            if case is None:
                warnings.append(f"{brief_path}: unparsed EXCLUDED_STRUCTURE")
                continue
            if case["case_id"] not in cases:
                cases[case["case_id"]] = case
                strata[case["stratum"]] += 1
    ordered = sorted(cases.values(), key=lambda item: (item["stratum"], item["case_id"]))
    if max_per_stratum is not None:
        if max_per_stratum < 1:
            raise ValueError("max_per_stratum must be positive")
        limited = []
        used: Counter[str] = Counter()
        for case in ordered:
            if used[case["stratum"]] >= max_per_stratum:
                continue
            used[case["stratum"]] += 1
            limited.append(case)
        ordered = limited
        strata = used
    result = {
        "schema_version": "1.1",
        "blind_protocol_version": DEFAULT_BLIND_PROTOCOL,
        "cases": ordered,
        "case_counts": dict(Counter(case["case_type"] for case in ordered)),
        "strata": dict(strata),
        "source_briefs": source_briefs,
        "warnings": warnings,
    }
    atomic_write(output_path, canonical_json(result) + "\n")
    if html_path is not None:
        atomic_write(html_path, blind_html(ordered))
    return result


def import_labels(input_path: Path, output_path: Path) -> dict:
    with input_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    validate_label_identities(rows)
    labels = []
    for row in rows:
        if "case_id" not in row or "annotator_id" not in row:
            raise ValueError("labels CSV requires case_id and annotator_id")
        direction = str(row.get("direction") or "ABSTAIN").strip().upper()
        if direction not in {"UP", "DOWN", "ABSTAIN"}:
            direction = "ABSTAIN"
        labels.append(
            {
                "case_id": str(row["case_id"]).strip(),
                "annotator_id": str(row["annotator_id"]).strip(),
                "selected_patterns": sorted(_selected(row.get("selected_patterns", ""))),
                "direction": direction,
                "abstain": _abstain(row),
                "split": row.get("split") or "ALL",
                "reason": row.get("reason", ""),
            }
        )
    result = {
        "schema_version": "1.1",
        "blind_protocol_version": DEFAULT_BLIND_PROTOCOL,
        "labels": labels,
    }
    atomic_write(output_path, canonical_json(result) + "\n")
    return result


def _abstain(row: dict) -> bool:
    value = row.get("abstain", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _selected(value) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {item.strip() for item in str(value).replace(",", "|").split("|") if item.strip()}


def _kappa(a: list[bool], b: list[bool]) -> float | None:
    if not a:
        return None
    po = sum(left == right for left, right in zip(a, b, strict=True)) / len(a)
    p1 = sum(a) / len(a)
    p2 = sum(b) / len(b)
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    return (po - pe) / (1 - pe) if pe < 1 else None


def _gate(
    double_cases: int,
    double_positive: int,
    double_negative: int,
    kappa: float | None,
) -> dict:
    reasons = []
    if double_cases < MIN_DOUBLE_CASES:
        reasons.append(f"double-valid cases {double_cases} < {MIN_DOUBLE_CASES}")
    if double_positive < MIN_DOUBLE_POSITIVE:
        reasons.append(f"double-positive cases {double_positive} < {MIN_DOUBLE_POSITIVE}")
    if double_negative < MIN_DOUBLE_NEGATIVE:
        reasons.append(f"double-negative cases {double_negative} < {MIN_DOUBLE_NEGATIVE}")
    if kappa is None:
        reasons.append("Cohen kappa unavailable")
    elif kappa < MIN_KAPPA:
        reasons.append(f"Cohen kappa {kappa:.3f} < {MIN_KAPPA:.2f}")
    if not reasons:
        return {"status": "PASS", "eligible_for_calibration": True, "reasons": []}
    counts_ok = (
        double_cases >= MIN_DOUBLE_CASES
        and double_positive >= MIN_DOUBLE_POSITIVE
        and double_negative >= MIN_DOUBLE_NEGATIVE
    )
    status = (
        "LOW_AGREEMENT"
        if counts_ok and kappa is not None and kappa < MIN_KAPPA
        else "INSUFFICIENT_EVIDENCE"
    )
    return {"status": status, "eligible_for_calibration": False, "reasons": reasons}


def _case_ids(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text())
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("cases must be a JSON list")
    result = {}
    for case in cases:
        if "case_id" not in case:
            raise ValueError("case is missing case_id")
        result[str(case["case_id"])] = str(case.get("split") or "ALL")
    return result


def _score_split(rows: list[dict], patterns: list[str], protocol_version: str) -> dict:
    by_case: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    valid_by_case = {
        case_id: [row for row in case_rows if not row["abstain"]]
        for case_id, case_rows in by_case.items()
    }
    valid_by_case = {
        case_id: case_rows for case_id, case_rows in valid_by_case.items() if case_rows
    }
    double_by_case = {
        case_id: case_rows for case_id, case_rows in valid_by_case.items() if len(case_rows) == 2
    }
    valid_rows = [row for case_rows in valid_by_case.values() for row in case_rows]
    abstain_count = sum(1 for row in rows if row["abstain"])
    empty_selection = sum(1 for row in valid_rows if not row["selected_patterns"])
    results = {}
    for pattern in patterns:
        recognition = [
            sum(pattern in row["selected_patterns"] for row in case_rows) / len(case_rows)
            for case_rows in valid_by_case.values()
        ]
        left: list[bool] = []
        right: list[bool] = []
        double_positive = 0
        double_negative = 0
        discordant: list[str] = []
        for case_id, case_rows in double_by_case.items():
            first = pattern in case_rows[0]["selected_patterns"]
            second = pattern in case_rows[1]["selected_patterns"]
            left.append(first)
            right.append(second)
            if first and second:
                double_positive += 1
            elif not first and not second:
                double_negative += 1
            else:
                discordant.append(case_id)
        positives = sum(pattern in row["selected_patterns"] for row in valid_rows)
        case_positive = sum(
            1
            for case_rows in valid_by_case.values()
            if any(pattern in row["selected_patterns"] for row in case_rows)
        )
        kappa = _kappa(left, right)
        results[pattern] = {
            "label": LABEL_KIND,
            "cases": len(double_by_case),
            "recognition_probability": (
                sum(recognition) / len(recognition) if recognition else None
            ),
            "recognition_cases": len(recognition),
            "double_valid_cases": len(double_by_case),
            "double_positive": double_positive,
            "double_negative": double_negative,
            "discordant_count": len(discordant),
            "discordant_cases": discordant,
            "positive_rate_left": sum(left) / len(left) if left else None,
            "positive_rate_right": sum(right) / len(right) if right else None,
            "annotation_positive_rate": positives / len(valid_rows) if valid_rows else None,
            "case_positive_rate": (case_positive / len(valid_by_case) if valid_by_case else None),
            "raw_agreement": (
                (double_positive + double_negative) / len(double_by_case)
                if double_by_case
                else None
            ),
            "cohen_kappa": kappa,
            "gate": _gate(len(double_by_case), double_positive, double_negative, kappa),
            "blind_protocol_version": protocol_version,
        }
    direction_by_case = {
        case_id: [row for row in case_rows if row["direction"] in {"UP", "DOWN"}]
        for case_id, case_rows in by_case.items()
    }
    double_direction = {
        case_id: case_rows
        for case_id, case_rows in direction_by_case.items()
        if len(case_rows) == 2
    }
    direction_left: list[bool] = []
    direction_right: list[bool] = []
    up_up = 0
    down_down = 0
    direction_discordant: list[str] = []
    for case_id, case_rows in double_direction.items():
        first = case_rows[0]["direction"] == "UP"
        second = case_rows[1]["direction"] == "UP"
        direction_left.append(first)
        direction_right.append(second)
        if first and second:
            up_up += 1
        elif not first and not second:
            down_down += 1
        else:
            direction_discordant.append(case_id)
    direction_kappa = _kappa(direction_left, direction_right)
    directions = {
        "counts": dict(Counter(row["direction"] for row in rows)),
        "double_valid_cases": len(double_direction),
        "up_up": up_up,
        "down_down": down_down,
        "discordant_count": len(direction_discordant),
        "discordant_cases": direction_discordant,
        "raw_agreement": (
            (up_up + down_down) / len(double_direction) if double_direction else None
        ),
        "cohen_kappa": direction_kappa,
    }
    return {
        "rows": len(rows),
        "cases": len(by_case),
        "cases_with_valid": len(valid_by_case),
        "cases_without_valid": len(by_case) - len(valid_by_case),
        "cases_with_extra_annotations": sum(
            1 for case_rows in valid_by_case.values() if len(case_rows) > 2
        ),
        "abstain": {
            "true": abstain_count,
            "false": len(rows) - abstain_count,
        },
        "abstain_count": abstain_count,
        "empty_selection": empty_selection,
        "directions": directions,
        "patterns": results,
    }


def score_labels(labels_path: Path, output_path: Path, cases_path: Path | None = None) -> dict:
    protocol_version = DEFAULT_BLIND_PROTOCOL
    if labels_path.suffix.lower() == ".json":
        payload = json.loads(labels_path.read_text())
        rows = payload.get("labels", payload) if isinstance(payload, dict) else payload
        if isinstance(payload, dict):
            protocol_version = str(
                payload.get(
                    "protocol_version",
                    payload.get("blind_protocol_version", DEFAULT_BLIND_PROTOCOL),
                )
            )
    else:
        with labels_path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
    validate_label_identities(rows)
    normalized = []
    for row in rows:
        if "case_id" not in row or "annotator_id" not in row:
            raise ValueError("labels require case_id and annotator_id")
        normalized.append(
            {
                "case_id": str(row["case_id"]).strip(),
                "annotator_id": str(row["annotator_id"]).strip(),
                "selected_patterns": _selected(row.get("selected_patterns", "")),
                "direction": (
                    str(row.get("direction") or "ABSTAIN").strip().upper()
                    if str(row.get("direction") or "ABSTAIN").strip().upper()
                    in {"UP", "DOWN", "ABSTAIN"}
                    else "ABSTAIN"
                ),
                "abstain": _abstain(row),
                "split": str(row.get("split") or "ALL"),
                "reason": row.get("reason", ""),
            }
        )
    case_splits = _case_ids(cases_path) if cases_path is not None else {}
    if case_splits:
        for row in normalized:
            if row["split"] == "ALL" and row["case_id"] in case_splits:
                row["split"] = case_splits[row["case_id"]]
    patterns = sorted(
        set(PATTERNS)
        | {
            pattern
            for row in normalized
            if not row["abstain"]
            for pattern in row["selected_patterns"]
        }
    )
    splits = sorted({row["split"] for row in normalized})
    if not splits:
        splits = sorted(set(case_splits.values())) or ["ALL"]
    all_result = _score_split(normalized, patterns, protocol_version)
    by_split = {
        split: _score_split(
            [row for row in normalized if row["split"] == split],
            patterns,
            protocol_version,
        )
        for split in splits
    }
    missing_ids = (
        sorted(set(case_splits) - {row["case_id"] for row in normalized}) if case_splits else None
    )
    output = {
        "schema_version": "1.1",
        "blind_protocol_version": protocol_version,
        "rows": len(normalized),
        "cases": all_result["cases"],
        "labeled_cases": all_result["cases"],
        "missing_cases": None if missing_ids is None else len(missing_ids),
        "missing_case_ids": missing_ids,
        "abstain": all_result["abstain"],
        "abstain_count": all_result["abstain_count"],
        "empty_selection": all_result["empty_selection"],
        "cases_without_valid": all_result["cases_without_valid"],
        "cases_with_extra_annotations": all_result["cases_with_extra_annotations"],
        "directions": all_result["directions"],
        "patterns": all_result["patterns"],
        "by_split": by_split,
    }
    atomic_write(output_path, canonical_json(output) + "\n")
    return output


def direction_labels(
    cases_path: Path,
    output_path: Path,
    *,
    cache_root: Path = Path(".cache/elliottwaver"),
    horizon: int | None = None,
) -> dict:
    payload = json.loads(cases_path.read_text())
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("cases must be a JSON list")
    store = SnapshotStore(cache_root)
    rows = []
    for case in cases:
        row = {
            "case_id": case.get("case_id"),
            "symbol": case.get("symbol"),
            "timeframe": case.get("timeframe"),
            "decision_date": case.get("decision_date"),
            "horizon": horizon or 5,
        }
        snapshot_id = case.get("source_snapshot_id")
        if not snapshot_id:
            row.update({"status": "UNDEFINED", "error": "missing source_snapshot_id"})
            rows.append(row)
            continue
        try:
            bundle = store.load(snapshot_id)
            timeframe = case.get("timeframe", "1d")
            if timeframe == "1w" and bundle.timeframe == "1d":
                bundle = as_weekly(bundle)
            decision = date.fromisoformat(case["decision_date"])
            size = horizon or 5
            window = outcome_window(bundle, decision, size)
            bar = window.close_bar(size)
            error = (
                None
                if bar is not None
                else window.missing(window.dates[-1]).status
                if len(window.dates) == size
                else "RIGHT_CENSORED"
            )
            decision_bars = (
                bundle.raw_bars if bundle.provenance.adjustment == "basis_date_qfq" else bundle.bars
            )
            decision_close = next(
                (b.close for b in decision_bars if b.session_date == decision), None
            )
        except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
            row.update({"status": "UNDEFINED", "error": f"{type(exc).__name__}: {exc}"})
            rows.append(row)
            continue
        if error is not None or bar is None or decision_close is None:
            row.update(
                {
                    "status": error or "UNDEFINED",
                    "decision_close": decision_close,
                }
            )
            rows.append(row)
            continue
        future_close = float(bar.close)
        if future_close > decision_close:
            status = "UP"
        elif future_close < decision_close:
            status = "DOWN"
        else:
            status = "FLAT"
        row.update(
            {
                "status": status,
                "horizon_date": bar.session_date.isoformat(),
                "decision_close": decision_close,
                "future_close": future_close,
                "return": future_close / decision_close - 1,
            }
        )
        rows.append(row)
    result = {
        "schema_version": "1.0",
        "horizon": horizon or 5,
        "cases": rows,
    }
    atomic_write(output_path, canonical_json(result) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    source = export.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--briefs", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--cache-root", type=Path, default=Path(".cache/elliottwaver"))
    export.add_argument("--bar-window", type=int, default=120)
    export.add_argument("--max-per-stratum", type=int)
    export.add_argument("--html", type=Path)
    import_parser = sub.add_parser("import")
    import_parser.add_argument("--input", type=Path, required=True)
    import_parser.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score")
    score.add_argument("--labels", type=Path, required=True)
    score.add_argument("--cases", type=Path)
    score.add_argument("--output", type=Path, required=True)
    direction = sub.add_parser("direction")
    direction.add_argument("--cases", type=Path, required=True)
    direction.add_argument("--output", type=Path, required=True)
    direction.add_argument("--cache-root", type=Path, default=Path(".cache/elliottwaver"))
    direction.add_argument("--horizon", type=int)
    predict = sub.add_parser("predict")
    predict.add_argument("--cases", type=Path, required=True)
    predict.add_argument("--model", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = (
            export_brief_cases(
                args.briefs,
                args.output,
                cache_root=args.cache_root,
                bar_window=args.bar_window,
                max_per_stratum=args.max_per_stratum,
                html_path=args.html,
            )
            if args.briefs
            else export_cases(args.input, args.output)
        )
    elif args.command == "import":
        result = import_labels(args.input, args.output)
    elif args.command == "direction":
        result = direction_labels(
            args.cases,
            args.output,
            cache_root=args.cache_root,
            horizon=args.horizon,
        )
    elif args.command == "predict":
        payload = json.loads(args.cases.read_text())
        cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
        result = {
            "schema_version": "1.0",
            "predictions": predict_label_probabilities(cases, load_label_model(args.model)),
        }
        atomic_write(args.output, canonical_json(result) + "\n")
    else:
        result = score_labels(args.labels, args.output, args.cases)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
