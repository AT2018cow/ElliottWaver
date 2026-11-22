from datetime import UTC, datetime

from conftest import make_pivots

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.projector import project_with_gates
from elliottwaver.engine.templates import IMPULSE, WXY
from elliottwaver.outlook import build_outlook
from elliottwaver.schemas.market import AnalysisRequest


def _as_of():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _candidate(template, prices, completed, stage):
    bars, points = make_pivots(prices)
    return bars, Candidate(template, points[:completed], points[-1], 1, stage, (), (1.4,))


def test_project_with_gates_assigns_impulse_advance_role():
    bars, candidate = _candidate(IMPULSE, [100, 120, 110], 2, "W2_DEVELOPING")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave == "2"
    assert projection.current_leg_direction == "DOWN"
    assert projection.advance_direction == "UP"
    roles = {(item.role, item.condition.kind) for item in projection.conditions}
    assert ("ADVANCE", "CONFIRMATION") in roles
    assert ("INVALIDATION", "INVALIDATION") in roles


def test_project_with_gates_assigns_completed_stage_exit_only():
    bars, candidate = _candidate(IMPULSE, [100, 120, 110, 150, 132, 160], 6, "COMPLETE")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave is None
    assert projection.current_leg_direction == "UNKNOWN"
    assert {item.role for item in projection.conditions} == {"STAGE_EXIT"}
    assert projection.zones


def test_project_with_gates_suppresses_double_zigzag_specific_evidence():
    bars, candidate = _candidate(WXY, [100, 120, 110, 130], 3, "Y_DEVELOPING")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
        variant="DOUBLE_THREE",
    )
    assert projection.zones == ()
    assert all(
        "双锯齿" not in item.condition.reason and "W终点" not in item.condition.reason
        for item in projection.conditions
    )


def test_build_outlook_has_five_sessions_and_cases():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.status == "AVAILABLE"
    assert outlook.horizon == 5
    assert outlook.horizon_unit == "SESSIONS"
    assert len(outlook.periods) == 5
    assert outlook.cases
    assert outlook.cases[0].slot == "PRIMARY"
    assert all(event.event_id for event in outlook.cases[0].events)


def test_build_outlook_is_unavailable_without_cards():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = build_outlook((), run.result, run.prepared, {})
    assert outlook.status == "UNAVAILABLE"
    assert "OUTLOOK_UNAVAILABLE" in (outlook.reason or "")


def test_project_with_gates_down_mirror():
    bars, points = make_pivots([200, 180, 190])
    candidate = Candidate(IMPULSE, points[:2], points[2], -1, "W2_DEVELOPING", (), (1.4,))
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave == "2"
    assert projection.current_leg_direction == "UP"
    assert projection.advance_direction == "DOWN"
    assert any(item.role == "ADVANCE" for item in projection.conditions)


def test_build_outlook_weekly_has_two_periods():
    bundle = demo_bundle("AAPL", timeframe="1w")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, timeframe="1w"),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.status == "AVAILABLE"
    assert outlook.horizon == 2
    assert outlook.horizon_unit == "WEEKS"
    assert len(outlook.periods) == 2
