"""Incremental settlement of issued brief events with revision history."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, StrictInt

from elliottwaver.data.adjustment import provider_price_scale
from elliottwaver.data.periods import as_weekly
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.engine.indicators import calculate
from elliottwaver.errors import DataError
from elliottwaver.evaluation.events import EVENT_SPECS, INTRADAY_EVENTS
from elliottwaver.evaluation.index_plans import apply_published_cancellation, settle_index_track
from elliottwaver.evaluation.outcomes import (
    Outcome,
    close_band,
    evaluate_outlook_event,
    first_touch,
    intraday_outlook_window,
    outcome_window,
    outlook_window,
)
from elliottwaver.forecast import ModelRegistry
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_plan import PlanTransition
from elliottwaver.schemas.market import AnalysisRequest, Contract, MarketDataBundle
from elliottwaver.schemas.outlook import OutlookCase
from elliottwaver.service import prepare_bundle
from elliottwaver.util import (
    canonical_json,
    digest,
    package_source_digest,
    support_test_inputs_digest,
)
from elliottwaver.visualization.brief_formatter import read_brief_report, read_issuance

NON_FINAL = {
    "RIGHT_CENSORED",
    "INCOMPLETE_DATA",
    "INPUT_UNAVAILABLE",
    "PRICE_BASIS_UNVERIFIED",
    "UNDEFINED_EVENT",
    "SETTLEMENT_CALENDAR_MISMATCH",
}
EVENT_ROWS = {
    "1d": (
        ("CLOSE_BAND_5D_V1", 5, "SESSIONS"),
        ("CLOSE_BAND_20D_V1", 20, "SESSIONS"),
    ),
    "1w": (
        ("CLOSE_BAND_5W_V1", 5, "WEEKS"),
        ("CLOSE_BAND_20W_V1", 20, "WEEKS"),
        ("CLOSE_BAND_2W_V1", 2, "WEEKS"),
    ),
}
CANDIDATE_EVENTS = {
    "1d": (
        ("CONFIRM_FIRST_20D_V1", 20, "SESSIONS"),
        ("TARGET_FIRST_20D_V1", 20, "SESSIONS"),
        ("ADVANCE_FIRST_5D_V1", 5, "SESSIONS"),
        ("OUTLOOK_TARGET_FIRST_5D_V1", 5, "SESSIONS"),
    ),
    "1w": (
        ("CONFIRM_FIRST_8W_V1", 8, "WEEKS"),
        ("TARGET_FIRST_8W_V1", 8, "WEEKS"),
        ("ADVANCE_FIRST_2W_V1", 2, "WEEKS"),
        ("OUTLOOK_TARGET_FIRST_2W_V1", 2, "WEEKS"),
    ),
}
SUPPORT_TESTS = (
    "tests/test_evaluation.py",
    "tests/test_forecasts.py",
    "tests/test_settlement.py",
)

for _timeframe, (_advance, _target) in INTRADAY_EVENTS.items():
    EVENT_ROWS[_timeframe] = ()
    CANDIDATE_EVENTS[_timeframe] = ((_advance, 5, "BARS"), (_target, 5, "BARS"))


def event_specs(brief: Brief) -> list[tuple[str, str, int, str]]:
    """Return (event_id, scope, horizon, unit) for one issued brief."""
    if brief.timeframe in INTRADAY_EVENTS:
        outlook = brief.outlook
        if outlook is None:
            return []
        if outlook.version != "1.1" or outlook.horizon_unit != "BARS":
            raise DataError("SETTLEMENT_WINDOW_TIMEFRAME_MISMATCH: 旧观察窗与主周期不匹配")
        if outlook.status != "AVAILABLE":
            return []
        case_ids = {case.scenario_id for case in outlook.cases}
        return [
            (event_id, card.scenario_id, horizon, unit)
            for card in brief.cards
            if card.scenario_id in case_ids
            for event_id, horizon, unit in CANDIDATE_EVENTS[brief.timeframe]
        ]
    if brief.timeframe not in EVENT_ROWS:
        raise DataError(f"Unsupported settlement timeframe: {brief.timeframe}")
    available = brief.outlook is not None and brief.outlook.status == "AVAILABLE"
    specs = [
        (event_id, brief.symbol, horizon, unit)
        for event_id, horizon, unit in EVENT_ROWS[brief.timeframe]
        if available or event_id != "CLOSE_BAND_2W_V1"
    ]
    if not available:
        # Old or unavailable reports keep exactly the event list they were issued with.
        return specs
    case_ids = {case.scenario_id for case in brief.outlook.cases}
    for card in brief.cards:
        if card.scenario_id not in case_ids:
            continue
        for event_id, horizon, unit in CANDIDATE_EVENTS[brief.timeframe]:
            specs.append((event_id, card.scenario_id, horizon, unit))
    return specs


def _event_status(outcome: Outcome) -> str:
    return outcome.status if outcome.status in NON_FINAL else "SETTLED"


def _source_hash(future) -> str:
    return digest([bar.model_dump(mode="json", exclude={"observed_at"}) for bar in future])


class SettlementStore:
    """SQLite store for issued event keys and immutable settlement revisions."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "settlements.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS settlements (
                    brief_digest TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    market_asof TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    horizon_unit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latest_revision INTEGER NOT NULL DEFAULT 0,
                    model_id TEXT,
                    probabilities TEXT,
                    brief_source_snapshot_id TEXT NOT NULL,
                    report_path TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (brief_digest, event_id, scope)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS settlement_revisions (
                    brief_digest TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    session TEXT,
                    value REAL,
                    settlement_snapshot_id TEXT NOT NULL,
                    settlement_input_snapshot_id TEXT,
                    price_coordinate_id TEXT,
                    evidence TEXT,
                    source_hash TEXT NOT NULL,
                    settled_at TEXT NOT NULL,
                    is_initial INTEGER NOT NULL,
                    supersedes_revision INTEGER,
                    PRIMARY KEY (brief_digest, event_id, scope, revision)
                )
                """
            )
            revision_columns = {
                row[1] for row in db.execute("PRAGMA table_info(settlement_revisions)")
            }
            if "evidence" not in revision_columns:
                db.execute("ALTER TABLE settlement_revisions ADD COLUMN evidence TEXT")
            for column in ("bar_id", "trigger_start_at", "trigger_end_at"):
                if column not in revision_columns:
                    db.execute(f"ALTER TABLE settlement_revisions ADD COLUMN {column} TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS settlement_symbol "
                "ON settlements(symbol, market_asof, event_id)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS index_plan_observations (
                    observation_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    brief_digest TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def settle_index_guidance(
        self,
        brief: Brief,
        source: MarketDataBundle,
        *,
        cancellations: dict[tuple[str, int], PlanTransition] | None = None,
    ) -> list[dict]:
        if brief.index_guidance is None:
            return []
        rows = []
        for track in brief.index_guidance.tracks:
            if track.plan is None:
                continue
            row = settle_index_track(track, source)
            cancellation = (cancellations or {}).get((track.plan.plan_id, track.plan.revision))
            if cancellation is not None and source.as_of >= cancellation.observable_at:
                row = apply_published_cancellation(row, cancellation)
            rows.append(row)
        with closing(sqlite3.connect(self.path)) as db:
            db.executemany(
                """
                INSERT INTO index_plan_observations
                (observation_id, plan_id, revision, brief_digest, source_snapshot_id, payload, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO NOTHING
                """,
                (
                    (
                        digest(row),
                        row["plan_id"],
                        row["revision"],
                        brief.brief_digest,
                        row["source_snapshot_id"],
                        canonical_json(row),
                        datetime.now(UTC).isoformat(),
                    )
                    for row in rows
                ),
            )
            db.commit()
        return rows

    def register_brief(self, brief: Brief, report_path: Path) -> int:
        """Insert pending keys for a brief; existing keys are left untouched."""
        report_path = report_path.resolve()
        brief, _ = read_brief_report(report_path, expected_digest=brief.brief_digest)
        published_at = read_issuance(report_path, brief)
        asset_type = route_symbol(brief.symbol).asset_type
        forecasts = {
            (item.event_id, item.scenario_id or brief.symbol): item for item in brief.forecasts
        }
        now = datetime.now(UTC).isoformat()
        inserted = 0
        with closing(sqlite3.connect(self.path)) as db:
            for event_id, scope, horizon, unit in event_specs(brief):
                forecast = forecasts.get((event_id, scope))
                cursor = db.execute(
                    """
                    INSERT INTO settlements
                    (brief_digest, event_id, scope, symbol, asset_type, timeframe,
                     market_asof, as_of, horizon, horizon_unit, status,
                     latest_revision, model_id, probabilities,
                     brief_source_snapshot_id, report_path, issued_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(brief_digest, event_id, scope) DO NOTHING
                    """,
                    (
                        brief.brief_digest,
                        event_id,
                        scope,
                        brief.symbol,
                        asset_type,
                        brief.timeframe,
                        brief.market_asof.isoformat(),
                        brief.as_of.isoformat(),
                        horizon,
                        unit,
                        forecast.model_id if forecast is not None else None,
                        (
                            canonical_json(forecast.probabilities)
                            if forecast is not None and forecast.probabilities
                            else None
                        ),
                        brief.source_snapshot_id,
                        str(report_path),
                        (published_at or brief.generated_at).isoformat(),
                        now,
                    ),
                )
                inserted += cursor.rowcount
            db.commit()
        return inserted

    def _row(self, db, brief_digest: str, event_id: str, scope: str):
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM settlements WHERE brief_digest = ? AND event_id = ? AND scope = ?",
            (brief_digest, event_id, scope),
        ).fetchone()

    def _latest_revision(self, db, brief_digest: str, event_id: str, scope: str):
        db.row_factory = sqlite3.Row
        return db.execute(
            """
            SELECT * FROM settlement_revisions
            WHERE brief_digest = ? AND event_id = ? AND scope = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (brief_digest, event_id, scope),
        ).fetchone()

    def _record(self, observation: dict) -> bool:
        key = (
            observation["brief_digest"],
            observation["event_id"],
            observation["scope"],
        )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, *key)
            if row is None:
                raise DataError("SETTLEMENT_KEY_NOT_REGISTERED")
            latest = self._latest_revision(db, *key)
            now = datetime.now(UTC).isoformat()
            if latest is not None:
                same = all(
                    latest[name] == observation[name]
                    for name in (
                        "status",
                        "outcome",
                        "session",
                        "value",
                        "evidence",
                        "source_hash",
                        "bar_id",
                        "trigger_start_at",
                        "trigger_end_at",
                    )
                )
                if same:
                    return False
                if latest["status"] == "SETTLED" and observation["status"] != "SETTLED":
                    return False
                revision = latest["revision"] + 1
                supersedes = latest["revision"]
                is_initial = 0
            else:
                revision = 1
                supersedes = None
                is_initial = 1
            db.execute(
                """
                INSERT INTO settlement_revisions
                (brief_digest, event_id, scope, revision, status, outcome, session,
                 value, settlement_snapshot_id, settlement_input_snapshot_id,
                 price_coordinate_id, evidence, source_hash, settled_at, is_initial,
                 supersedes_revision, bar_id, trigger_start_at, trigger_end_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *key,
                    revision,
                    observation["status"],
                    observation["outcome"],
                    observation["session"],
                    observation["value"],
                    observation["settlement_snapshot_id"],
                    observation["settlement_input_snapshot_id"],
                    observation["price_coordinate_id"],
                    observation["evidence"],
                    observation["source_hash"],
                    now,
                    is_initial,
                    supersedes,
                    observation["bar_id"],
                    observation["trigger_start_at"],
                    observation["trigger_end_at"],
                ),
            )
            db.execute(
                """
                UPDATE settlements
                SET status = ?, latest_revision = ?, updated_at = ?
                WHERE brief_digest = ? AND event_id = ? AND scope = ?
                """,
                (observation["status"], revision, now, *key),
            )
            db.commit()
        return True

    def settle_brief(
        self, brief: Brief, report_path: Path, source_bundle: MarketDataBundle
    ) -> list[dict]:
        brief, result = read_brief_report(Path(report_path), expected_digest=brief.brief_digest)
        if (
            source_bundle.instrument.canonical_symbol != brief.symbol
            or source_bundle.provenance.feed != brief.feed
            or source_bundle.provenance.adjustment != brief.adjustment
        ):
            raise DataError("SETTLEMENT_SOURCE_MISMATCH")
        settlement_snapshot_id = source_bundle.snapshot_id
        if brief.timeframe == "1w" and source_bundle.timeframe == "1d":
            source_bundle = as_weekly(source_bundle)
        if source_bundle.timeframe != brief.timeframe:
            raise DataError("SETTLEMENT_TIMEFRAME_MISMATCH")
        atr = brief.decision_atr
        issuance_factors = None
        provider_bridge = (
            brief.adjustment == "provider_all"
            and settlement_snapshot_id != brief.source_snapshot_id
        )
        price_scale = 1.0
        if atr is None or brief.adjustment == "basis_date_qfq" or provider_bridge:
            try:
                snapshots = SnapshotStore(self.root)
                try:
                    issued = snapshots.load(brief.input_snapshot_id)
                except DataError:
                    issued = prepare_bundle(
                        AnalysisRequest(
                            symbol=brief.symbol,
                            as_of=brief.as_of,
                            end=brief.market_asof,
                            mode=brief.mode,
                            timeframe=brief.timeframe,
                        ),
                        snapshots.load(brief.source_snapshot_id),
                    )
                if (
                    issued.snapshot_id != brief.input_snapshot_id
                    or issued.provenance.price_coordinate_id != brief.price_coordinate_id
                ):
                    raise DataError("SETTLEMENT_ISSUANCE_MISMATCH")
                if atr is None:
                    atr = float(calculate(issued.bars).atr[-1])
                issuance_factors = issued.factors
                if provider_bridge:
                    price_scale = provider_price_scale(issued, source_bundle, brief.market_asof)
            except DataError:
                if not provider_bridge:
                    raise
                price_scale = None
        if brief.outlook is not None:
            if (
                brief.outlook.price_coordinate_id is not None
                and brief.outlook.price_coordinate_id != brief.price_coordinate_id
            ):
                raise DataError("SETTLEMENT_ISSUANCE_MISMATCH")
            if brief.outlook.issued_at is not None and (
                brief.outlook.issued_at < brief.as_of
                or (
                    brief.mode != "LIVE"
                    and brief.timeframe not in INTRADAY_EVENTS
                    and brief.outlook.issued_at != brief.as_of
                )
            ):
                raise DataError("SETTLEMENT_ISSUANCE_MISMATCH")
        scenarios = {scenario.scenario_id: scenario for scenario in result.scenarios}
        for event_id, scope, horizon, _unit in event_specs(brief):
            if price_scale is None:
                outcome = Outcome("PRICE_BASIS_UNVERIFIED")
                used = ()
            else:
                frozen_event = event_id == "CLOSE_BAND_2W_V1" or event_id.startswith(
                    ("ADVANCE_FIRST_", "OUTLOOK_TARGET_FIRST_")
                )
                if frozen_event and brief.outlook is not None:
                    if brief.timeframe in INTRADAY_EVENTS:
                        window = intraday_outlook_window(
                            source_bundle,
                            brief.market_asof,
                            brief.outlook.periods,
                            horizon,
                            issuance_factors=issuance_factors,
                            price_scale=price_scale,
                        )
                    else:
                        window = outlook_window(
                            source_bundle,
                            brief.market_asof,
                            brief.outlook.periods,
                            horizon,
                            issuance_factors=issuance_factors,
                            price_scale=price_scale,
                        )
                else:
                    window = outcome_window(
                        source_bundle,
                        brief.market_asof,
                        horizon,
                        issuance_factors=issuance_factors,
                        issuance_dates=brief.outcome_dates,
                        price_scale=price_scale,
                    )
                if event_id.startswith("CLOSE_BAND_"):
                    outcome = close_band(window, brief.latest_price, atr, horizon)
                    used = (window.close_bar(horizon),) if outcome.value is not None else ()
                else:
                    scenario = scenarios.get(scope)
                    if event_id.startswith(("ADVANCE_FIRST_", "OUTLOOK_TARGET_FIRST_")):
                        case = next(
                            (
                                OutlookCase.model_validate(item)
                                for item in (
                                    brief.outlook.cases if brief.outlook is not None else ()
                                )
                                if item.scenario_id == scope
                            ),
                            None,
                        )
                        if case is None:
                            outcome = Outcome("UNDEFINED_EVENT")
                        else:
                            outcome = evaluate_outlook_event(EVENT_SPECS[event_id], case, window)
                            if outcome.status == "UNDEFINED_EVENT":
                                state = next(
                                    (item for item in case.events if item.event_id == event_id),
                                    None,
                                )
                                if state is not None and state.reason:
                                    outcome = Outcome("UNDEFINED_EVENT", evidence=(state.reason,))
                    else:
                        outcome = (
                            Outcome("UNDEFINED_EVENT")
                            if scenario is None
                            else first_touch(
                                scenario,
                                window,
                                horizon,
                                confirmation=event_id.startswith("CONFIRM_FIRST_"),
                            )
                        )
                    used = window.observed_bars(outcome)
            observation = {
                "brief_digest": brief.brief_digest,
                "event_id": event_id,
                "scope": scope,
                "status": _event_status(outcome),
                "outcome": outcome.status,
                "session": outcome.session.isoformat() if outcome.session else None,
                "value": outcome.value,
                "settlement_snapshot_id": settlement_snapshot_id,
                "settlement_input_snapshot_id": source_bundle.snapshot_id,
                "price_coordinate_id": brief.price_coordinate_id,
                "evidence": (canonical_json(list(outcome.evidence)) if outcome.evidence else None),
                "source_hash": _source_hash(bar for bar in used if bar is not None),
                "bar_id": outcome.bar_id,
                "trigger_start_at": outcome.start_at.isoformat() if outcome.start_at else None,
                "trigger_end_at": outcome.end_at.isoformat() if outcome.end_at else None,
            }
            self._record(observation)
        return self.rows(brief_digest=brief.brief_digest)

    def revisions(self, brief_digest: str, event_id: str, scope: str) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM settlement_revisions
                WHERE brief_digest = ? AND event_id = ? AND scope = ?
                ORDER BY revision
                """,
                (brief_digest, event_id, scope),
            ).fetchall()
        return [dict(row) for row in rows]

    def rows(self, *, brief_digest: str | None = None, symbol: str | None = None) -> list[dict]:
        clauses = []
        values: list[object] = []
        if brief_digest is not None:
            clauses.append("brief_digest = ?")
            values.append(brief_digest)
        if symbol is not None:
            clauses.append("symbol = ?")
            values.append(symbol)
        query = "SELECT * FROM settlements"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY symbol, market_asof, event_id, scope"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            history = self.revisions(item["brief_digest"], item["event_id"], item["scope"])
            item["revisions"] = history
            item["initial"] = history[0] if history else None
            item["latest"] = history[-1] if history else None
            result.append(item)
        return result


class SupportTestReceipt(Contract):
    schema_version: Literal["1.0"] = "1.0"
    source_sha256: str
    test_inputs_sha256: str | None = None
    command: tuple[str, ...]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    exit_code: StrictInt


def _support_tests(directory: Path | None, at: datetime) -> tuple[dict, dict]:
    statuses = dict.fromkeys(SUPPORT_TESTS, "UNVERIFIED")
    evidence = {}
    if directory is None:
        return statuses, evidence
    source_sha256 = package_source_digest()
    try:
        test_inputs_sha256 = support_test_inputs_digest()
    except OSError:
        test_inputs_sha256 = None
    if test_inputs_sha256 is None:
        return statuses, evidence
    for name in SUPPORT_TESTS:
        path = directory / (Path(name).stem + ".json")
        try:
            receipt = SupportTestReceipt.model_validate_json(path.read_text())
        except (OSError, ValueError):
            continue
        if (
            receipt.source_sha256 == source_sha256
            and receipt.test_inputs_sha256 == test_inputs_sha256
            and receipt.command[1:] == ("-m", "pytest", name, "-q")
            and receipt.started_at <= receipt.completed_at <= at
        ):
            statuses[name] = "PASS" if receipt.exit_code == 0 else "FAIL"
            evidence[name] = receipt.model_dump(mode="json")
    return statuses, evidence


def support_matrix(
    model_summary: dict | None = None,
    *,
    registry: ModelRegistry | None = None,
    brief: Brief | None = None,
    at: datetime | None = None,
    test_receipts: Path | None = None,
) -> dict:
    """Describe event support, research reports and verified operational applicability."""
    summary = model_summary or {}
    cohorts = list(summary["cohorts"].values()) if "cohorts" in summary else [summary]
    when = at or datetime.now(UTC)
    tests, test_evidence = _support_tests(test_receipts, when)
    instrument = route_symbol(brief.symbol) if brief is not None else None
    active = {}
    applicable = {}
    if (
        registry is not None
        and brief is not None
        and not brief.retrospective
        and when >= brief.as_of
    ):
        active = {
            item["model_id"]: item
            for item in registry.active_models_at(
                at or brief.as_of,
                engine_version=brief.engine_version.removesuffix("/hierarchical_v1").rsplit("/", 1)[
                    0
                ],
                config_hash=brief.config_hash,
                timeframe=brief.timeframe,
                asset_type=instrument.asset_type,
                market=instrument.market,
                symbol=brief.symbol,
                feed=brief.feed,
                adjustment=brief.adjustment,
            )
        }
        for forecast in brief.forecasts:
            record = active.get(forecast.model_id)
            if record is None or forecast.status != "APPROVED":
                continue
            model = record["model"]
            if model["models"].get(forecast.event_id, {}).get("status") != "APPROVED":
                continue
            spec = EVENT_SPECS.get(forecast.event_id)
            if (
                spec is None
                or spec.timeframe != brief.timeframe
                or spec.horizon != forecast.horizon_sessions
                or spec.unit != forecast.horizon_unit
            ):
                continue
            scope = model.get("scope") or {}
            if scope.get("outlook_slot") not in {None, forecast.outlook_slot} or scope.get(
                "outlook_version"
            ) not in {None, forecast.outlook_version}:
                continue
            applicable.setdefault(forecast.event_id, []).append(
                {
                    "model_id": forecast.model_id,
                    "outlook_slot": forecast.outlook_slot,
                    "outlook_version": forecast.outlook_version,
                    "scenario_id": forecast.scenario_id,
                    "approved_at": record["approved_at"],
                    "scope": scope,
                }
            )
    rows = []
    for asset_type in ("EQUITY", "INDEX"):
        for timeframe, events in EVENT_ROWS.items():
            compatible = [
                cohort
                for cohort in cohorts
                if (
                    not cohort.get("scope")
                    or (
                        asset_type in cohort["scope"].get("asset_types", [])
                        and timeframe in cohort["scope"].get("timeframes", [])
                    )
                )
            ] or [{}]
            for cohort in compatible:
                for event_id, horizon, unit in (*events, *CANDIDATE_EVENTS[timeframe]):
                    entry = cohort.get("models", {}).get(event_id, {})
                    research_status = entry.get("status", "UNCALIBRATED")
                    reasons = entry.get("reasons") or []
                    context_matches = (
                        brief is not None
                        and instrument.asset_type == asset_type
                        and brief.timeframe == timeframe
                    )
                    scope = cohort.get("scope") or {}
                    context_matches = context_matches and ModelRegistry._compatible(
                        {"scope": scope},
                        market=instrument.market if instrument else None,
                        symbol=brief.symbol if brief else None,
                        feed=brief.feed if brief else None,
                        adjustment=brief.adjustment if brief else None,
                    )
                    verified = (
                        [
                            item
                            for item in applicable.get(event_id, [])
                            if scope.get("outlook_slot") in {None, item["outlook_slot"]}
                            and scope.get("outlook_version") in {None, item["outlook_version"]}
                        ]
                        if context_matches
                        else []
                    )
                    status = (
                        "APPROVED"
                        if verified
                        else (
                            "INSUFFICIENT_EVIDENCE"
                            if research_status == "INSUFFICIENT_EVIDENCE"
                            else "UNCALIBRATED"
                        )
                    )
                    rows.append(
                        {
                            "asset_type": asset_type,
                            "timeframe": timeframe,
                            "event_id": event_id,
                            "horizon": horizon,
                            "horizon_unit": unit,
                            "scope": cohort.get("scope"),
                            "outlook_version": (cohort.get("scope") or {}).get("outlook_version"),
                            "outlook_slot": (cohort.get("scope") or {}).get("outlook_slot"),
                            "symbols": (cohort.get("scope") or {}).get("symbols"),
                            "model_id": verified[0]["model_id"] if verified else None,
                            "model_status": status,
                            "applicable_model": bool(verified),
                            "applicable_models": verified,
                            "research_status": research_status,
                            "research_model_id": cohort.get("model_id"),
                            "implemented": True,
                            "data_status": "REPORT_LINKED" if context_matches else "UNVERIFIED",
                            "tests": tests,
                            "reason": None
                            if status == "APPROVED"
                            else (", ".join(map(str, reasons)) or "未注册批准模型"),
                        }
                    )
    return {
        "schema_version": "1.1",
        "research_model_id": summary.get("model_id"),
        "brief_digest": brief.brief_digest if brief is not None else None,
        "model_checked_at": (at or brief.as_of).isoformat() if brief is not None else None,
        "test_evidence": test_evidence,
        "rows": rows,
    }
