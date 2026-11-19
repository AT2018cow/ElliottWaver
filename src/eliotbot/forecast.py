"""Approved forecast-model loading and brief inference."""

from __future__ import annotations

import fcntl
import json
import math
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from eliotbot.evaluation.calibration import predict
from eliotbot.evaluation.features import (
    FEATURE_VERSION,
    candidate_features,
    decision_features,
    event_feature_order,
    feature_vector,
)
from eliotbot.schemas.brief import BriefForecast
from eliotbot.util import atomic_write, digest

CLOSE_EVENTS = {
    "CLOSE_BAND_5D_V1",
    "CLOSE_BAND_20D_V1",
    "CLOSE_BAND_5W_V1",
    "CLOSE_BAND_20W_V1",
}
CANDIDATE_EVENTS = {
    "CONFIRM_FIRST_20D_V1",
    "TARGET_FIRST_20D_V1",
    "CONFIRM_FIRST_8W_V1",
    "TARGET_FIRST_8W_V1",
}


def event_horizon(event_id: str) -> int:
    if "20D" in event_id or "20W" in event_id:
        return 20
    if "8W" in event_id:
        return 8
    return 5


def _horizon_unit(event_id: str) -> str:
    return "WEEKS" if event_id.endswith("W_V1") else "SESSIONS"


def _finite(values) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def validate_model(model: dict, *, operational: bool = False) -> dict:
    models = model.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("MODEL_INVALID: models must be a non-empty mapping")
    release = model.get("release")
    if operational and any(
        entry.get("status") == "APPROVED" for entry in models.values() if isinstance(entry, dict)
    ):
        scope = model.get("scope") or {}
        if (
            model.get("feature_version") != FEATURE_VERSION
            or model.get("schema_version") != "1.2"
            or not release
            or not all(
                model.get(key)
                for key in (
                    "splits",
                    "engine_version",
                    "config_hash",
                    "protocol_sha256",
                    "decisions_sha256",
                    "events_sha256",
                )
            )
            or not all(
                isinstance(scope.get(key), list) and len(scope[key]) == 1
                for key in ("markets", "asset_types", "timeframes", "feed", "adjustment")
            )
        ):
            raise ValueError(
                "MODEL_INVALID: missing current release evidence or single-cohort scope"
            )
    for event_id, entry in models.items():
        if not isinstance(entry, dict):
            raise ValueError(f"MODEL_INVALID: {event_id} entry must be an object")
        status = entry.get("status")
        if status not in {
            "APPROVED",
            "INSUFFICIENT_EVIDENCE",
            "OUT_OF_SCOPE",
            "MODEL_INVALID",
        }:
            raise ValueError(f"MODEL_INVALID: unsupported status for {event_id}")
        if status != "APPROVED":
            continue
        feature_order = entry.get("feature_order")
        classes = entry.get("classes")
        mean = entry.get("mean")
        scale = entry.get("scale")
        weights = entry.get("weights")
        intercept = entry.get("intercept")
        if not isinstance(feature_order, list) or not isinstance(classes, list):
            raise ValueError(f"MODEL_INVALID: missing feature_order/classes for {event_id}")
        if event_id not in CLOSE_EVENTS | CANDIDATE_EVENTS:
            raise ValueError(f"MODEL_INVALID: unknown event {event_id}")
        if operational and feature_order != event_feature_order(event_id):
            raise ValueError(f"MODEL_INVALID: feature order mismatch for {event_id}")
        if len(classes) not in {2, 3}:
            raise ValueError(f"MODEL_INVALID: unsupported class count for {event_id}")
        if event_id in CLOSE_EVENTS and classes != ["UP", "RANGE", "DOWN"]:
            raise ValueError(f"MODEL_INVALID: class set mismatch for {event_id}")
        if event_id in CANDIDATE_EVENTS and classes != ["NO", "YES"]:
            raise ValueError(f"MODEL_INVALID: class set mismatch for {event_id}")
        if not isinstance(mean, list) or len(mean) != len(feature_order):
            raise ValueError(f"MODEL_INVALID: mean dimension mismatch for {event_id}")
        if not isinstance(scale, list) or len(scale) != len(feature_order):
            raise ValueError(f"MODEL_INVALID: scale dimension mismatch for {event_id}")
        if (
            not isinstance(weights, list)
            or len(weights) != len(feature_order)
            or any(not isinstance(row, list) or len(row) != len(classes) for row in weights)
        ):
            raise ValueError(f"MODEL_INVALID: weight dimension mismatch for {event_id}")
        if not isinstance(intercept, list) or len(intercept) != len(classes):
            raise ValueError(f"MODEL_INVALID: intercept dimension mismatch for {event_id}")
        values = list(mean) + list(scale) + list(intercept)
        for row in weights:
            values.extend(row)
        if not _finite(values):
            raise ValueError(f"MODEL_INVALID: non-finite parameter for {event_id}")
        if any(float(value) <= 0 for value in scale):
            raise ValueError(f"MODEL_INVALID: non-positive scale for {event_id}")
        if (
            not _finite([entry.get("temperature", 1.0)])
            or float(entry.get("temperature", 1.0)) <= 0
        ):
            raise ValueError(f"MODEL_INVALID: non-positive temperature for {event_id}")
        support = entry.get("support_bins")
        if support is not None:
            if not isinstance(support, dict) or set(support) != set(classes):
                raise ValueError(f"MODEL_INVALID: support_bins class mismatch for {event_id}")
            for class_name, bins in support.items():
                if not isinstance(bins, list):
                    raise ValueError(f"MODEL_INVALID: support_bins for {class_name} must be a list")
                for item in bins:
                    low = float(item["low"])
                    high = float(item["high"])
                    if not (0.0 <= low < high <= 1.0):
                        raise ValueError(f"MODEL_INVALID: invalid support bin for {event_id}")
        if release:
            reasons = entry.get("reasons")
            if not isinstance(reasons, list) or reasons:
                raise ValueError(f"MODEL_INVALID: APPROVED model has gate reasons for {event_id}")
            improvement = entry.get("improvement")
            if not isinstance(improvement, dict):
                raise ValueError(f"MODEL_INVALID: missing gate improvement for {event_id}")
            for baseline_name in ("frequency", "momentum", "price"):
                baseline = improvement.get(baseline_name)
                if not isinstance(baseline, dict):
                    raise ValueError(
                        f"MODEL_INVALID: missing {baseline_name} baseline for {event_id}"
                    )
                for key in ("brier_ci", "log_loss_ci"):
                    interval = baseline.get(key)
                    if (
                        not isinstance(interval, list)
                        or len(interval) != 2
                        or not _finite(interval)
                        or not 0 < float(interval[0]) <= float(interval[1])
                    ):
                        raise ValueError(
                            f"MODEL_INVALID: {baseline_name} {key} does not clear zero for {event_id}"
                        )
            coverage = float(entry.get("supported_coverage", 0))
            if coverage < float(release.get("min_supported_coverage", 0)):
                raise ValueError(f"MODEL_INVALID: supported coverage below gate for {event_id}")
            if operational:
                for key, gate in (
                    ("test_rows", "min_test_rows"),
                    ("test_dates", "min_test_dates"),
                    ("test_symbols", "min_test_symbols"),
                ):
                    if entry.get(key, 0) < release[gate]:
                        raise ValueError(f"MODEL_INVALID: {key} below gate for {event_id}")
                for supported in (support or {}).values():
                    if not supported:
                        raise ValueError(f"MODEL_INVALID: empty support for {event_id}")
                    for item in supported:
                        ci = item.get("calibration_gap_ci")
                        if (
                            item.get("count", 0) < release["min_bin_count"]
                            or item.get("dates", 0) < release["min_bin_dates"]
                            or item.get("occupied_blocks", 0) < 2
                            or not isinstance(ci, list)
                            or len(ci) != 2
                            or not -release["reliability_tolerance"]
                            <= ci[0]
                            <= ci[1]
                            <= release["reliability_tolerance"]
                        ):
                            raise ValueError(
                                f"MODEL_INVALID: unverified support bin for {event_id}"
                            )
            if not entry.get("support_bins"):
                raise ValueError(f"MODEL_INVALID: missing support_bins for {event_id}")
    return model


def load_model(path: Path, *, operational: bool = False) -> dict:
    model = json.loads(path.read_text())
    if not isinstance(model, dict):
        raise ValueError("MODEL_INVALID: the model file must contain an object")
    recorded = model.get("model_id")
    if recorded != digest({key: value for key, value in model.items() if key != "model_id"}):
        raise ValueError("MODEL_INVALID: model_id does not match content")
    return validate_model(model, operational=operational)


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_approval_time(model: dict, value) -> datetime:
    approved_at = _parse_time(value)
    try:
        holdout_end = date.fromisoformat(model["splits"]["test"]["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("MODEL_INVALID: missing valid holdout end") from exc
    if approved_at.date() <= holdout_end:
        raise ValueError("MODEL_INVALID: approval precedes the completed holdout")
    return approved_at


def _iso_time(value) -> str:
    return _parse_time(value or datetime.now(UTC)).isoformat()


class ModelRegistry:
    """Approved model registry with current/previous fallback."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "registry.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": "1.0", "scopes": {}}
        return json.loads(self.path.read_text())

    def _write(self, value: dict) -> None:
        atomic_write(self.path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "registry.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _seed_history(entry: dict) -> list[dict]:
        if "history" in entry:
            return list(entry["history"])
        history = []
        previous = entry.get("previous")
        current = entry.get("current")
        if previous:
            history.append(
                {
                    "model_id": previous["model_id"],
                    "path": previous["path"],
                    "action": "APPROVED",
                    "at": previous.get("registered_at", datetime.now(UTC).isoformat()),
                }
            )
        if current:
            history.append(
                {
                    "model_id": current["model_id"],
                    "path": current["path"],
                    "action": "APPROVED",
                    "at": current.get("registered_at", datetime.now(UTC).isoformat()),
                }
            )
        withdrawn_at = current.get("registered_at") if current else datetime.now(UTC).isoformat()
        for model_id in entry.get("withdrawn", []):
            record = next(
                (item for item in (previous, current) if item and item["model_id"] == model_id),
                None,
            )
            history.append(
                {
                    "model_id": model_id,
                    "path": record["path"] if record else "",
                    "action": "WITHDRAWN",
                    "at": withdrawn_at,
                }
            )
        return history

    def register(self, model_path: Path, scope: str = "default", *, approved_at=None) -> dict:
        model = load_model(model_path, operational=True)
        if not any(entry.get("status") == "APPROVED" for entry in model["models"].values()):
            raise ValueError("MODEL_INVALID: no APPROVED event")
        with self._lock():
            registry = self._read()
            entries = registry.setdefault("scopes", {})
            entry = entries.setdefault(scope, {})
            entry["history"] = self._seed_history(entry)
            previous = entry.get("current")
            at = validate_approval_time(model, approved_at or datetime.now(UTC)).isoformat()
            entry["current"] = {
                "model_id": model["model_id"],
                "path": str(model_path.resolve()),
                "registered_at": at,
            }
            entry["previous"] = previous
            entry["withdrawn"] = [
                item for item in entry.get("withdrawn", []) if item != model["model_id"]
            ]
            entry["history"].append(
                {
                    "model_id": model["model_id"],
                    "path": str(model_path.resolve()),
                    "action": "APPROVED",
                    "at": at,
                }
            )
            registry["schema_version"] = "1.1"
            self._write(registry)
            return entry

    def withdraw(self, model_id: str, scope: str = "default", *, withdrawn_at=None) -> dict:
        with self._lock():
            registry = self._read()
            entry = registry.get("scopes", {}).get(scope)
            if entry is None:
                raise ValueError("MODEL_NOT_FOUND")
            entry["history"] = self._seed_history(entry)
            at = _iso_time(withdrawn_at)
            path = ""
            for event in reversed(entry["history"]):
                if event["model_id"] == model_id and event["action"] == "APPROVED":
                    path = event.get("path", "")
                    break
            withdrawn = list(entry.get("withdrawn", []))
            if model_id not in withdrawn:
                withdrawn.append(model_id)
            if (entry.get("current") or {}).get("model_id") == model_id:
                entry["current"] = entry.get("previous")
                entry["previous"] = None
            entry["withdrawn"] = withdrawn
            entry["history"].append(
                {
                    "model_id": model_id,
                    "path": path,
                    "action": "WITHDRAWN",
                    "at": at,
                }
            )
            registry["schema_version"] = "1.1"
            self._write(registry)
            return entry

    def timeline(self, scope: str = "default") -> list[dict]:
        entry = self._read().get("scopes", {}).get(scope)
        if entry is None:
            return []
        return sorted(self._seed_history(entry), key=lambda event: _parse_time(event["at"]))

    @staticmethod
    def _compatible(
        model: dict,
        *,
        engine_version: str | None = None,
        config_hash: str | None = None,
        timeframe: str | None = None,
        asset_type: str | None = None,
        feed: str | None = None,
        adjustment: str | None = None,
        market: str | None = None,
    ) -> bool:
        if (
            engine_version
            and model.get("engine_version")
            and model["engine_version"] != engine_version
        ):
            return False
        if config_hash and model.get("config_hash") and model["config_hash"] != config_hash:
            return False
        scope_info = model.get("scope") or {}
        if market and scope_info.get("markets") and market not in scope_info["markets"]:
            return False
        if timeframe and scope_info.get("timeframes") and timeframe not in scope_info["timeframes"]:
            return False
        if (
            asset_type
            and scope_info.get("asset_types")
            and asset_type not in scope_info["asset_types"]
        ):
            return False
        if feed and scope_info.get("feed") and feed not in scope_info["feed"]:
            return False
        if (
            adjustment
            and scope_info.get("adjustment")
            and adjustment not in scope_info["adjustment"]
        ):
            return False
        return True

    def _load_record(self, record: dict, **filters) -> dict | None:
        try:
            model = load_model(Path(record["path"]), operational=True)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if model.get("model_id") != record.get("model_id"):
            return None
        return model if self._compatible(model, **filters) else None

    def active_at(self, at, scope: str = "default", **filters) -> dict | None:
        """Return the latest model approved and not withdrawn at ``at``."""
        when = _parse_time(at)
        entry = self._read().get("scopes", {}).get(scope)
        if entry is None:
            return None
        events = self._seed_history(entry)
        active: dict[str, dict] = {}
        for event in sorted(events, key=lambda item: _parse_time(item["at"])):
            event_at = _parse_time(event["at"])
            if event_at > when:
                break
            if event["action"] == "APPROVED":
                active[event["model_id"]] = event
            else:
                active.pop(event["model_id"], None)
        candidates = sorted(
            active.values(), key=lambda event: _parse_time(event["at"]), reverse=True
        )
        for event in candidates:
            model = self._load_record(event, **filters)
            if model is not None:
                return {
                    "model": model,
                    "model_id": model["model_id"],
                    "approved_at": event["at"],
                    "path": event.get("path"),
                }
        return None

    def load_at(self, at, scope: str = "default", **filters) -> dict | None:
        active = self.active_at(at, scope, **filters)
        return None if active is None else active["model"]

    def load(self, scope: str = "default", **filters) -> dict | None:
        return self.load_at(datetime.now(UTC), scope, **filters)


def _bin_supported(value: float, supported: list[dict]) -> bool:
    for item in supported:
        if item["low"] <= value < item["high"] or (item["high"] == 1.0 and value == 1.0):
            return True
    return False


def forecasts_for_brief(
    result,
    prepared,
    indicators,
    model: dict | None,
    *,
    qualified=None,
    weekly=None,
    candidate_ids: set[str] | None = None,
) -> tuple[BriefForecast, ...]:
    if model is None:
        return ()
    scenarios = result.scenarios if qualified is None else qualified
    decision = decision_features(result, prepared, indicators, qualified=scenarios, weekly=weekly)
    atr = float(indicators.atr[-1])
    forecasts = []
    for event_id, entry in model["models"].items():
        if (result.timeframe, _horizon_unit(event_id)) not in {("1d", "SESSIONS"), ("1w", "WEEKS")}:
            continue
        candidates = event_id in CANDIDATE_EVENTS
        targets = (
            [
                (rank, scenario)
                for rank, scenario in enumerate(scenarios, 1)
                if candidate_ids is None or scenario.scenario_id in candidate_ids
            ]
            if candidates
            else [(0, None)]
        )
        for rank, scenario in targets or [(0, None)]:
            attrs = dict(
                event_id=event_id,
                horizon_sessions=event_horizon(event_id),
                horizon_unit=_horizon_unit(event_id),
                model_id=model.get("model_id"),
                scenario_id=scenario.scenario_id if scenario else None,
                sample_count=entry.get("test_rows"),
                calibration_end=((model.get("splits") or {}).get("calibration") or {}).get("end"),
                scope=model.get("scope"),
            )
            status = entry["status"]
            if status != "APPROVED":
                forecasts.append(
                    BriefForecast(
                        **attrs,
                        status=status,
                        reason=entry.get("reason") or ", ".join(entry.get("reasons", [])) or None,
                    )
                )
                continue
            if result.status not in {"OK", "UNCLEAR"} or not math.isfinite(atr) or atr <= 0:
                forecasts.append(
                    BriefForecast(
                        **attrs, status="INSUFFICIENT_EVIDENCE", reason="决策输入不足或搜索未完成"
                    )
                )
                continue
            if candidates and (
                scenario is None
                or not any(c.kind == "INVALIDATION" for c in scenario.conditions)
                or (
                    not any(c.kind == "CONFIRMATION" for c in scenario.conditions)
                    if event_id.startswith("CONFIRM_")
                    else not scenario.target_zones
                )
            ):
                forecasts.append(
                    BriefForecast(
                        **attrs, status="UNDEFINED_EVENT", reason="缺少定义事件所需的条件"
                    )
                )
                continue
            candidate = (
                candidate_features(scenario, prepared.bars[-1].close, atr, rank=rank)
                if scenario
                else None
            )
            vector = feature_vector(entry["feature_order"], decision, candidate)
            normal = (np.asarray([vector]) - np.asarray(entry["mean"])) / np.asarray(entry["scale"])
            weights = np.asarray(entry["weights"]).reshape(len(vector), len(entry["classes"]))
            probabilities = predict(
                normal, weights, np.asarray(entry["intercept"]), entry.get("temperature", 1.0)
            )[0]
            if not np.isfinite(probabilities).all():
                forecasts.append(
                    BriefForecast(**attrs, status="MODEL_INVALID", reason="模型返回非有限值")
                )
                continue
            probability_map = dict(zip(entry["classes"], map(float, probabilities), strict=True))
            support = entry.get("support_bins")
            if support is not None and any(
                not _bin_supported(value, support.get(name, []))
                for name, value in probability_map.items()
            ):
                forecasts.append(
                    BriefForecast(
                        **attrs, status="UNSUPPORTED_BIN", reason="概率落入未获支持的档位"
                    )
                )
                continue
            forecasts.append(
                BriefForecast(**attrs, status="APPROVED", probabilities=probability_map)
            )
    return tuple(forecasts)
