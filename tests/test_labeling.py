import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from label_wave_cases import (
    direction_labels,
    export_brief_cases,
    export_cases,
    import_labels,
    score_labels,
)

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.util import digest
from elliottwaver.visualization.brief_formatter import publish_brief


def test_export_removes_hidden_fields(tmp_path):
    source = tmp_path / "cases.json"
    source.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "c1",
                        "symbol": "AAPL",
                        "timeframe": "1d",
                        "decision_date": "2025-01-01",
                        "degree": "MINOR",
                        "endpoints": [1, 2, 3],
                        "future_bars": [1, 2, 3],
                        "model_choice": "IMPULSE",
                    }
                ]
            }
        )
    )
    result = export_cases(source, tmp_path / "blind.json")
    case = result["cases"][0]
    assert case["case_id"] == "c1"
    assert "future_bars" not in case
    assert "model_choice" not in case


def test_export_brief_cases_from_published_brief(bundle, tmp_path):
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    reports = tmp_path / "reports"
    publish_brief(run, reports, formats=("md",))
    SnapshotStore(tmp_path / ".cache/elliottwaver").save(run.source)

    result = export_brief_cases(
        reports,
        tmp_path / "cases.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
        html_path=tmp_path / "blind.html",
    )

    assert result["cases"]
    assert result["strata"]
    case = result["cases"][0]
    assert case["symbol"] == "AAPL"
    assert case["case_type"] in {"CANDIDATE", "EXCLUDED_STRUCTURE"}
    assert case["ambiguity"] in {"LOW", "MEDIUM", "HIGH"}
    assert case["stratum"].endswith(f"|{case['ambiguity']}")
    assert case["endpoints"]
    assert case["bars"]
    assert case["bars_available"] is True
    assert len(case["bars"]) <= 30
    assert "pattern_type" not in case
    assert "name" not in case
    assert "role" not in case
    assert "scenario_id" not in case
    page = (tmp_path / "blind.html").read_text()
    assert "<svg" in page
    assert case["case_id"] in page
    assert "IMPULSE" in page
    assert "name='direction-1'" in page
    assert "name='reviewed'" in page
    assert "pattern_type" not in page
    assert "scenario_id" not in page


def test_export_brief_cases_dedupes_and_limits(bundle, tmp_path):
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    reports = tmp_path / "reports"
    path = publish_brief(run, reports, formats=("md",))
    SnapshotStore(tmp_path / ".cache/elliottwaver").save(run.source)
    shutil.copytree(path, reports / "copy")

    result = export_brief_cases(
        reports,
        tmp_path / "cases.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
        max_per_stratum=1,
    )
    assert len(result["cases"]) == len(result["strata"])
    assert all(count == 1 for count in result["strata"].values())
    assert len(result["source_briefs"]) == 1


def test_import_labels_and_score_json(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "case_id,annotator_id,selected_patterns,abstain,reason\n"
        "c1,a,IMPULSE,false,\n"
        "c1,b,IMPULSE,false,\n"
        "c2,a,,false,\n"
        "c2,b,,false,\n"
    )
    imported = import_labels(labels, tmp_path / "labels.json")
    assert imported["labels"][0]["selected_patterns"] == ["IMPULSE"]
    scored = score_labels(tmp_path / "labels.json", tmp_path / "score.json")
    assert scored["patterns"]["IMPULSE"]["cases"] == 2
    assert scored["patterns"]["IMPULSE"]["raw_agreement"] == 1.0


def test_score_labels_computes_agreement_and_kappa(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "case_id,annotator_id,selected_patterns,abstain,reason\n"
        "c1,a,IMPULSE,false,\n"
        "c1,b,IMPULSE,false,\n"
        "c2,a,IMPULSE,false,\n"
        "c2,b,IMPULSE,false,\n"
        "c3,a,,false,\n"
        "c3,b,,false,\n"
        "c4,a,ZIGZAG_ABC,false,\n"
        "c4,b,,false,\n"
        "c5,a,IMPULSE,true,\n"
        "c5,b,IMPULSE,false,\n"
    )
    result = score_labels(labels, tmp_path / "score.json")
    assert result["cases"] == 5
    assert result["patterns"]["IMPULSE"]["cases"] == 4
    assert result["patterns"]["IMPULSE"]["raw_agreement"] == 1.0
    assert result["patterns"]["IMPULSE"]["cohen_kappa"] == 1.0
    assert result["patterns"]["ZIGZAG_ABC"]["raw_agreement"] == 0.75
    assert result["patterns"]["ZIGZAG_ABC"]["cohen_kappa"] == 0.0


def _write_labels(path: Path, rows: list[dict]) -> None:
    fields = [
        "case_id",
        "annotator_id",
        "selected_patterns",
        "direction",
        "abstain",
        "split",
        "reason",
    ]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_case_equal_recognition_probability_preserves_all_judgments(tmp_path):
    labels = tmp_path / "labels.csv"
    _write_labels(
        labels,
        [
            {
                "case_id": "c1",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c1",
                "annotator_id": "b",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c2",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c2",
                "annotator_id": "b",
                "selected_patterns": "",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c2",
                "annotator_id": "c",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c3",
                "annotator_id": "a",
                "selected_patterns": "",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c4",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "abstain": "true",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c4",
                "annotator_id": "b",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
        ],
    )
    result = score_labels(labels, tmp_path / "score.json")
    entry = result["patterns"]["IMPULSE"]
    assert entry["recognition_probability"] == pytest.approx(2 / 3)
    assert entry["recognition_cases"] == 4
    assert entry["double_valid_cases"] == 1
    assert entry["double_positive"] == 1
    assert entry["double_negative"] == 0
    assert entry["cohen_kappa"] is None
    assert entry["gate"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["cases_with_extra_annotations"] == 1
    assert result["abstain_count"] == 1
    assert result["empty_selection"] == 2
    assert result["by_split"]["train"]["patterns"]["IMPULSE"][
        "recognition_probability"
    ] == pytest.approx(2 / 3)


def test_recognition_gate_passes_at_50_10_10_kappa(tmp_path):
    rows = []
    for index in range(10):
        case_id = f"positive-{index}"
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": case_id,
                    "annotator_id": annotator,
                    "selected_patterns": "IMPULSE",
                    "abstain": "false",
                    "split": "train",
                    "reason": "",
                }
            )
    for index in range(40):
        case_id = f"negative-{index}"
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": case_id,
                    "annotator_id": annotator,
                    "selected_patterns": "",
                    "abstain": "false",
                    "split": "train",
                    "reason": "",
                }
            )
    labels = tmp_path / "labels.csv"
    _write_labels(labels, rows)
    result = score_labels(labels, tmp_path / "score.json")
    entry = result["patterns"]["IMPULSE"]
    assert entry["label"] == "INDEPENDENT_RECOGNITION"
    assert entry["double_valid_cases"] == 50
    assert entry["double_positive"] == 10
    assert entry["double_negative"] == 40
    assert entry["recognition_probability"] == pytest.approx(0.2)
    assert entry["annotation_positive_rate"] == pytest.approx(0.2)
    assert entry["case_positive_rate"] == pytest.approx(0.2)
    assert entry["raw_agreement"] == 1.0
    assert entry["cohen_kappa"] == pytest.approx(1.0)
    assert entry["gate"] == {
        "status": "PASS",
        "eligible_for_calibration": True,
        "reasons": [],
    }


def test_recognition_gate_rejects_low_agreement(tmp_path):
    rows = []
    for index in range(10):
        case_id = f"positive-{index}"
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": case_id,
                    "annotator_id": annotator,
                    "selected_patterns": "IMPULSE",
                    "abstain": "false",
                    "split": "train",
                    "reason": "",
                }
            )
    for index in range(10):
        case_id = f"negative-{index}"
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": case_id,
                    "annotator_id": annotator,
                    "selected_patterns": "",
                    "abstain": "false",
                    "split": "train",
                    "reason": "",
                }
            )
    for index in range(30):
        case_id = f"discordant-{index}"
        rows.append(
            {
                "case_id": case_id,
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "train",
                "reason": "",
            }
        )
        rows.append(
            {
                "case_id": case_id,
                "annotator_id": "b",
                "selected_patterns": "",
                "abstain": "false",
                "split": "train",
                "reason": "",
            }
        )
    labels = tmp_path / "labels.csv"
    _write_labels(labels, rows)
    result = score_labels(labels, tmp_path / "score.json")
    entry = result["patterns"]["IMPULSE"]
    assert entry["double_valid_cases"] == 50
    assert entry["double_positive"] == 10
    assert entry["double_negative"] == 10
    assert entry["discordant_count"] == 30
    assert entry["cohen_kappa"] < 0.60
    assert entry["gate"]["status"] == "LOW_AGREEMENT"
    assert entry["gate"]["eligible_for_calibration"] is False
    assert any("Cohen kappa" in reason for reason in entry["gate"]["reasons"])


def test_score_keeps_splits_separate_and_reports_missing(tmp_path):
    cases = {
        "cases": [
            *[{"case_id": f"train-{index}", "split": "train"} for index in range(50)],
            *[{"case_id": f"test-{index}", "split": "test"} for index in range(5)],
            {"case_id": "missing", "split": "test"},
        ]
    }
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases))
    rows = []
    for index in range(10):
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": f"train-{index}",
                    "annotator_id": annotator,
                    "selected_patterns": "IMPULSE",
                    "abstain": "false",
                    "split": "",
                    "reason": "",
                }
            )
    for index in range(10, 50):
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": f"train-{index}",
                    "annotator_id": annotator,
                    "selected_patterns": "",
                    "abstain": "false",
                    "split": "",
                    "reason": "",
                }
            )
    for index in range(5):
        for annotator in ("a", "b"):
            rows.append(
                {
                    "case_id": f"test-{index}",
                    "annotator_id": annotator,
                    "selected_patterns": "",
                    "abstain": "false",
                    "split": "",
                    "reason": "",
                }
            )
    labels = tmp_path / "labels.csv"
    _write_labels(labels, rows)
    result = score_labels(labels, tmp_path / "score.json", cases_path)
    assert result["by_split"]["train"]["patterns"]["IMPULSE"]["gate"]["status"] == "PASS"
    assert (
        result["by_split"]["test"]["patterns"]["IMPULSE"]["gate"]["status"]
        == "INSUFFICIENT_EVIDENCE"
    )
    assert result["missing_cases"] == 1
    assert result["missing_case_ids"] == ["missing"]


def _published_brief(bundle, tmp_path):
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    SnapshotStore(tmp_path / ".cache/elliottwaver").save(run.source)
    return run, path


def _write_variant(reports, source_path, brief, name):
    destination = reports / name
    destination.mkdir(parents=True)
    brief["brief_digest"] = digest(
        {key: value for key, value in brief.items() if key not in {"brief_digest", "generated_at"}}
    )
    (destination / "brief.json").write_text(json.dumps(brief, ensure_ascii=False))
    shutil.copy(source_path / "daily.json", destination / "daily.json")
    manifest = json.loads((source_path / "manifest.json").read_text())
    manifest["brief_digest"] = brief["brief_digest"]
    manifest["files"] = {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in destination.iterdir()
    }
    (destination / "manifest.json").write_text(json.dumps(manifest))


def test_export_includes_no_candidate_case(bundle, tmp_path):
    _run, path = _published_brief(bundle, tmp_path)
    brief = json.loads((path / "brief.json").read_text())
    brief["cards"] = []
    brief["status"] = "UNCLEAR"
    brief["issues"] = [
        {
            "code": "NO_QUALIFIED_CANDIDATE",
            "scope": "analysis",
            "message": "当前没有达到质量与证据门槛的候选",
        }
    ]
    reports = tmp_path / "no-candidate-reports"
    _write_variant(reports, path, brief, "case")
    result = export_brief_cases(
        reports,
        tmp_path / "no-candidate.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
    )
    case = next(item for item in result["cases"] if item["case_type"] == "NO_CANDIDATE")
    assert case["endpoints"] == []
    assert case["endpoints_complete"] is True
    assert case["ambiguity"] == "HIGH"
    assert case["stratum"] == "EQUITY|UNKNOWN|UNKNOWN|HIGH"
    assert case["bars"]
    assert result["case_counts"] == {"NO_CANDIDATE": 1}


def test_export_includes_excluded_structure_case(bundle, tmp_path):
    _run, path = _published_brief(bundle, tmp_path)
    brief = json.loads((path / "brief.json").read_text())
    brief["issues"].append(
        {
            "code": "EXCLUDED_STRUCTURE",
            "scope": "analysis",
            "message": (
                "ZIGZAG_ABC/COMPLETE q=5.4 cov=0.71 reason=quality; "
                "2024-12-17→2025-01-30 0=1383.20 A=1498.07 B=1396.66 C=1479.93"
            ),
        }
    )
    reports = tmp_path / "excluded-reports"
    _write_variant(reports, path, brief, "case")
    result = export_brief_cases(
        reports,
        tmp_path / "excluded.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
    )
    case = next(
        item
        for item in result["cases"]
        if item["case_type"] == "EXCLUDED_STRUCTURE" and item["endpoints"][0]["price"] == 1383.20
    )
    assert [point["label"] for point in case["endpoints"]] == ["1", "2", "3", "4"]
    assert case["endpoints"][0]["date"] == "2024-12-17"
    assert case["endpoints"][-1]["date"] == "2025-01-30"
    assert case["endpoints"][1]["kind"] == "HIGH"
    assert case["endpoints"][2]["kind"] == "LOW"
    assert case["endpoints_complete"] is False
    assert "ENDPOINT_DATES_PARTIAL" in case["warnings"]
    assert "pattern_type" not in case
    assert "rejection_reason" not in case
    assert "quality_score" not in case
    assert case["ambiguity"] == "HIGH"


def test_export_stratifies_by_ambiguity(bundle, tmp_path):
    _run, path = _published_brief(bundle, tmp_path)
    brief = json.loads((path / "brief.json").read_text())
    brief["cross_timeframe"]["state"] = "CONFLICT"
    brief["window_comparison"]["state"] = "CHANGED_BIAS"
    reports = tmp_path / "ambiguous-reports"
    _write_variant(reports, path, brief, "case")
    result = export_brief_cases(
        reports,
        tmp_path / "ambiguous.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
    )
    cards = [item for item in result["cases"] if item["case_type"] == "CANDIDATE"]
    assert cards
    assert all(item["ambiguity"] == "HIGH" for item in cards)
    assert all(item["stratum"].endswith("|HIGH") for item in cards)


def test_direction_labels_use_future_bars(bundle, tmp_path):
    decision = bundle.bars[-30]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=decision.usable_at,
            end=decision.session_date,
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    publish_brief(run, tmp_path / "reports", formats=("md",))
    SnapshotStore(tmp_path / ".cache/elliottwaver").save(run.source)
    cases = export_brief_cases(
        tmp_path / "reports",
        tmp_path / "cases.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        bar_window=30,
    )
    result = direction_labels(
        tmp_path / "cases.json",
        tmp_path / "directions.json",
        cache_root=tmp_path / ".cache/elliottwaver",
        horizon=5,
    )
    assert result["cases"]
    row = result["cases"][0]
    assert row["status"] in {"UP", "DOWN", "FLAT"}
    assert row["horizon_date"]
    assert row["decision_close"] > 0
    assert row["future_close"] > 0
    assert row["case_id"] == cases["cases"][0]["case_id"]


def test_score_reports_direction_agreement(tmp_path):
    labels = tmp_path / "labels.csv"
    _write_labels(
        labels,
        [
            {
                "case_id": "c1",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "direction": "UP",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c1",
                "annotator_id": "b",
                "selected_patterns": "IMPULSE",
                "direction": "UP",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c2",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "direction": "DOWN",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c2",
                "annotator_id": "b",
                "selected_patterns": "IMPULSE",
                "direction": "DOWN",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c3",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "direction": "UP",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
            {
                "case_id": "c3",
                "annotator_id": "b",
                "selected_patterns": "IMPULSE",
                "direction": "DOWN",
                "abstain": "false",
                "split": "train",
                "reason": "",
            },
        ],
    )
    result = score_labels(labels, tmp_path / "score.json")
    directions = result["directions"]
    assert directions["double_valid_cases"] == 3
    assert directions["up_up"] == 1
    assert directions["down_down"] == 1
    assert directions["discordant_count"] == 1
    assert directions["raw_agreement"] == pytest.approx(2 / 3)
    assert directions["cohen_kappa"] == pytest.approx(0.4)


def test_import_preserves_split_and_protocol(tmp_path):
    labels = tmp_path / "labels.csv"
    _write_labels(
        labels,
        [
            {
                "case_id": "c1",
                "annotator_id": "a",
                "selected_patterns": "IMPULSE",
                "abstain": "false",
                "split": "test",
                "reason": "",
            }
        ],
    )
    imported = import_labels(labels, tmp_path / "labels.json")
    assert imported["blind_protocol_version"] == "1.0"
    assert imported["labels"][0]["split"] == "test"

    payload = json.loads((tmp_path / "labels.json").read_text())
    payload["blind_protocol_version"] = "2.0"
    (tmp_path / "labels-v2.json").write_text(json.dumps(payload))
    scored = score_labels(tmp_path / "labels-v2.json", tmp_path / "score-v2.json")
    assert scored["blind_protocol_version"] == "2.0"
