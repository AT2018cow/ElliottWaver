import json

import pytest

from elliottwaver.label_models import (
    case_target,
    load_label_model,
    predict_label_probabilities,
    train_label_models,
)


def _case(case_id, split, positive, index):
    return {
        "case_id": case_id,
        "symbol": f"S{index % 20}",
        "timeframe": "1d",
        "decision_date": f"2025-01-{1 + index % 20:02d}",
        "asset_type": "EQUITY" if index % 2 else "INDEX",
        "degree": "MINOR" if index % 2 else "INTERMEDIATE",
        "direction": "UP" if index % 3 else "DOWN",
        "ambiguity": "HIGH" if positive else "LOW",
        "case_type": "CANDIDATE",
        "endpoints": [1, 2, 3],
        "bars_available": True,
        "source_snapshot_id": "a" * 64,
        "split": split,
    }


def _labels(case, positive):
    return [
        {
            "case_id": case["case_id"],
            "annotator_id": annotator,
            "selected_patterns": ["IMPULSE"] if positive else [],
            "direction": "UP" if positive else "DOWN",
            "abstain": False,
            "split": case["split"],
        }
        for annotator in ("a", "b")
    ]


def _write_dataset(tmp_path, train=50, calibration=50, test=200):
    cases = []
    labels = []
    for split, count, positive_count in (
        ("train", train, 10),
        ("calibration", calibration, 10),
        ("test", test, 50),
    ):
        for index in range(count):
            positive = index < positive_count
            case = _case(f"{split}-{index}", split, positive, index)
            cases.append(case)
            labels.extend(_labels(case, positive))
    cases_path = tmp_path / "cases.json"
    labels_path = tmp_path / "labels.json"
    cases_path.write_text(json.dumps({"cases": cases}))
    labels_path.write_text(json.dumps({"labels": labels}))
    return cases_path, labels_path


def test_train_label_models_approves_synthetic_signal(tmp_path):
    cases_path, labels_path = _write_dataset(tmp_path)
    summary = train_label_models(cases_path, labels_path, tmp_path / "models")
    entry = summary["models"]["IMPULSE"]
    assert entry["status"] == "APPROVED", entry["reasons"]
    assert entry["label_gate"]["status"] == "PASS"
    assert entry["label_gate"]["double_positive"] == 10
    assert entry["label_gate"]["double_negative"] == 40
    assert entry["test_cases"] == 200
    assert entry["test_dates"] >= 10
    assert entry["test_symbols"] >= 5
    assert entry["supported_coverage"] == 1.0
    assert entry["support_bins"]
    assert entry["improvement"]["frequency"]["brier_ci"][0] > 0
    assert entry["improvement"]["degree"]["brier_ci"][0] > 0

    model = load_label_model(tmp_path / "models" / "label-model.json")
    assert model["models"]["IMPULSE"]["status"] == "APPROVED"
    predictions = predict_label_probabilities(
        [
            _case("positive", "test", True, 0),
            _case("negative", "test", False, 1),
        ],
        model,
    )
    assert predictions[0]["probabilities"]["IMPULSE"] > 0.5
    assert predictions[1]["probabilities"]["IMPULSE"] < 0.5


def test_train_label_models_rejects_insufficient_labels(tmp_path):
    cases = [_case(f"case-{index}", "train", index < 2, index) for index in range(10)]
    labels = []
    for case in cases:
        labels.extend(_labels(case, case["case_id"] in {"case-0", "case-1"}))
    cases_path = tmp_path / "cases.json"
    labels_path = tmp_path / "labels.json"
    cases_path.write_text(json.dumps({"cases": cases}))
    labels_path.write_text(json.dumps({"labels": labels}))
    summary = train_label_models(cases_path, labels_path, tmp_path / "models")
    entry = summary["models"]["IMPULSE"]
    assert entry["status"] == "INSUFFICIENT_EVIDENCE"
    assert any("double-valid" in reason for reason in entry["reasons"])
    assert not (tmp_path / "models" / "label-model.json").exists() or entry["status"] != "APPROVED"


def test_train_label_models_rejects_case_spanning_splits(tmp_path):
    case = _case("case", "train", True, 0)
    labels = _labels(case, True)
    labels[1]["split"] = "test"
    cases_path = tmp_path / "cases.json"
    labels_path = tmp_path / "labels.json"
    cases_path.write_text(json.dumps({"cases": [case]}))
    labels_path.write_text(json.dumps({"labels": labels}))
    with pytest.raises(ValueError, match="spans multiple splits"):
        train_label_models(cases_path, labels_path, tmp_path / "models")


def test_case_target_is_case_equal():
    case = {
        "labels": [
            {"selected_patterns": {"IMPULSE"}, "abstain": False},
            {"selected_patterns": set(), "abstain": False},
            {"selected_patterns": {"IMPULSE"}, "abstain": False},
        ]
    }
    assert case_target(case, "IMPULSE") == pytest.approx(2 / 3)
    assert (
        case_target(
            {"labels": [{"selected_patterns": {"IMPULSE"}, "abstain": False}]},
            "IMPULSE",
        )
        == 1.0
    )
    assert (
        case_target(
            {"labels": [{"selected_patterns": set(), "abstain": True}]},
            "IMPULSE",
        )
        is None
    )
