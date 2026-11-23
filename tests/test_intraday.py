import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

import elliottwaver.brief as brief_module
import elliottwaver.engine.intraday_link as intraday_link_module
from elliottwaver.brief import build_brief, compare_decisions
from elliottwaver.cli.main import _choose_brief_timeframe, app
from elliottwaver.config import Settings
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import bar_window, load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import intraday_windows, resample_bundle
from elliottwaver.data.quality import missing_sessions
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.engine.hierarchical import IntervalMatch
from elliottwaver.engine.intraday_link import link_intraday_timeframes
from elliottwaver.engine.matcher import SearchBudget
from elliottwaver.evaluation.decisions import evaluate_protocol
from elliottwaver.evaluation.events import EVENT_SPECS
from elliottwaver.evaluation.outcomes import (
    OutcomeWindow,
    evaluate_outlook_event,
    intraday_outlook_window,
)
from elliottwaver.evaluation.replay import replay
from elliottwaver.history import HistoryStore
from elliottwaver.schemas.market import AnalysisRequest, Bar, FactorPoint, MarketDataBundle
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition, OutlookPeriod
from elliottwaver.schemas.wave import LevelCondition, PivotPoint, PriceZone, WaveStructure
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.settlement import SettlementStore, event_specs
from elliottwaver.visualization.brief_formatter import publish_brief


def _run(symbol: str, timeframe: str):
    bundle = demo_bundle(symbol, timeframe=timeframe)
    request = AnalysisRequest(
        symbol=symbol,
        as_of=bundle.as_of,
        timeframe=timeframe,
        mode="FROZEN_DATA_REPLAY",
    )
    return bundle, build_brief(request, bundle, Settings(), include_weekly=False)


def test_cn_60m_brief_uses_five_future_bars_not_weeks():
    bundle, run = _run("600519.SH", "60m")
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.version == "1.1"
    assert outlook.horizon_unit == "BARS"
    assert outlook.horizon == 5
    assert len(outlook.periods) == 5
    assert all(period.start_at >= bundle.as_of for period in outlook.periods)
    assert [period.start_at for period in outlook.periods] == sorted(
        period.start_at for period in outlook.periods
    )
    event_ids = {event.event_id for case in outlook.cases for event in case.events}
    assert "ADVANCE_FIRST_60M_5B_V1" in event_ids
    assert "OUTLOOK_TARGET_FIRST_60M_5B_V1" in event_ids
    assert not any("2W" in event_id for event_id in event_ids)


def test_us_5m_brief_uses_five_future_bars_on_the_same_session():
    bundle, run = _run("AAPL", "5m")
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.horizon_unit == "BARS"
    assert len(outlook.periods) == 5
    assert len({period.end_session for period in outlook.periods}) == 1
    starts = [period.start_at for period in outlook.periods]
    assert starts == [bundle.as_of + timedelta(minutes=5 * index) for index in range(5)]
    assert run.brief.last_bar_end_at == bundle.bars[-1].end_at
    assert run.brief.last_bar_id == bundle.bars[-1].bar_id


def test_cn_60m_outlook_crosses_lunch_break_in_scheduled_order():
    _bundle, run = _run("600519.SH", "60m")
    periods = run.brief.outlook.periods
    first = periods[0]
    second = periods[1]
    assert first.start_at.hour == 6  # 14:00 CST
    assert second.start_at.hour == 1  # next session 09:30 CST
    assert second.start_at.minute == 30


def test_intraday_timestamp_anchor_selects_the_exact_bar():
    bundle = demo_bundle("AAPL", timeframe="5m")
    bar = bundle.bars[-20]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.as_of,
            timeframe="5m",
            anchor=bar.session_date,
            anchor_at=bar.start_at,
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    detail = run.brief.window.anchor_details[0]
    assert detail.at == bar.start_at
    assert run.result.diagnostics.anchor_times == (bar.start_at,)
    assert run.brief.window.start_at == bar.start_at
    assert run.brief.window.end_at == bundle.bars[-1].end_at


def test_background_pairs_resample_from_the_same_source():
    cn_1m = demo_bundle("600519.SH", timeframe="1m")
    cn_5m = resample_bundle(cn_1m, "5m", as_of=cn_1m.as_of)
    assert cn_5m.timeframe == "5m"
    assert cn_5m.provenance.aggregation == "1m->5m"
    assert cn_5m.bars and cn_5m.raw_bars

    us_5m = demo_bundle("AAPL", timeframe="5m")
    us_60m = resample_bundle(us_5m, "60m", as_of=us_5m.as_of)
    assert us_60m.timeframe == "60m"
    assert us_60m.provenance.aggregation == "5m->60m"
    assert us_60m.bars and us_60m.raw_bars

    cn_60m = demo_bundle("600519.SH", timeframe="60m")
    cn_1d = resample_bundle(cn_60m, "1d", as_of=cn_60m.as_of)
    assert cn_1d.timeframe == "1d"
    assert cn_1d.provenance.aggregation == "60m->1d"
    assert cn_1d.bars and cn_1d.raw_bars


def test_background_resample_refuses_an_incomplete_coarse_window():
    bundle = demo_bundle("AAPL", timeframe="5m")
    bars = list(bundle.bars)
    removed = bars.pop(len(bars) // 2)
    broken = bundle.model_copy(update={"bars": tuple(bars), "raw_bars": tuple(bars)})
    result = resample_bundle(broken, "60m", as_of=broken.as_of)
    assert all(not (bar.start_at <= removed.start_at < bar.end_at) for bar in result.bars)


def test_daily_background_waits_for_native_bars_and_preserves_missing_sessions():
    hourly = demo_bundle("AAPL", timeframe="60m")
    derived = resample_bundle(hourly, "1d")
    missing_day = derived.bars[len(derived.bars) // 2].session_date
    assert derived.bars[0].session_date < missing_day < derived.bars[-1].session_date
    broken = hourly.model_copy(
        update={
            field: tuple(bar for bar in getattr(hourly, field) if bar.session_date != missing_day)
            for field in ("bars", "raw_bars")
        }
    )
    assert missing_day in missing_sessions(resample_bundle(broken, "1d"))

    last = derived.bars[-1]
    calendar = load_calendar(
        hourly.instrument, last.session_date - timedelta(days=120), last.session_date
    )
    daily = demo_bundle("AAPL")
    native = tuple(
        item.model_copy(
            update={
                "start_at": opened,
                "end_at": closed,
                "available_at": available,
                "session_date": session.date,
            }
        )
        for session, item in zip(
            (session for session in calendar.sessions if session.date <= last.session_date),
            daily.bars,
            strict=False,
        )
        for opened, closed, available in (bar_window(session, "US"),)
    )
    assert native[-1].usable_at > last.end_at
    bundle = hourly.model_copy(
        update={
            "as_of": max(native[-1].usable_at, hourly.as_of),
            "daily_context": native,
            "raw_daily_context": native,
        }
    )
    request = AnalysisRequest(symbol="AAPL", as_of=last.end_at, timeframe="60m")
    settings = Settings()
    run = build_brief(request, bundle, settings, check_windows=False)
    assert run.brief.background.market_asof == native[-2].session_date
    _, prepared = AnalysisService(settings).background(request, bundle)
    assert prepared.bars == native[:-1]
    assert prepared.provenance.coverage_reason == "NATIVE_DAILY_BACKGROUND"
    assert run.brief.background.input_snapshot_id == prepared.snapshot_id
    unavailable = build_brief(request, hourly, settings, check_windows=False)
    assert unavailable.brief.background_state == "INSUFFICIENT_DATA"
    assert any("NATIVE_DAILY_UNAVAILABLE" in issue.message for issue in unavailable.brief.issues)


def test_snapshot_identity_ignores_default_provenance_coverage_fields():
    bundle = demo_bundle("AAPL")
    payload = bundle.model_dump(mode="json")
    for key in (
        "source_timeframe",
        "aggregation",
        "coverage_start",
        "coverage_end",
        "coverage_reason",
        "asset_id",
    ):
        payload["provenance"].pop(key, None)
    legacy = MarketDataBundle.model_validate(payload)
    assert legacy.snapshot_id == bundle.snapshot_id


def test_hierarchical_engine_accepts_intraday_timeframes():
    bundle = demo_bundle("AAPL", timeframe="5m")
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1"},
        search={"max_subwave_depth": 1},
    )
    result = AnalysisService(settings).analyze_bundle(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.as_of,
            timeframe="5m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
    )
    assert result.status == "OK"
    assert result.schema_version == "1.1"
    assert result.scenarios
    assert result.scenarios[0].structure is not None


def test_intraday_parent_child_linker_returns_typed_relations():
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1"},
        search={"max_subwave_depth": 1},
    )
    service = AnalysisService(settings)
    child = demo_bundle("AAPL", timeframe="5m")
    parent = demo_bundle("AAPL", timeframe="60m")
    child_request = AnalysisRequest(
        symbol="AAPL",
        as_of=child.as_of,
        timeframe="5m",
        mode="FROZEN_DATA_REPLAY",
    )
    parent_request = AnalysisRequest(
        symbol="AAPL",
        as_of=parent.as_of,
        timeframe="60m",
        mode="FROZEN_DATA_REPLAY",
    )
    child_context = service.analyze_with_context(
        child_request, prepare_bundle(child_request, child)
    )
    parent_context = service.analyze_with_context(
        parent_request, prepare_bundle(parent_request, parent)
    )
    relations = link_intraday_timeframes(
        parent_context,
        prepare_bundle(parent_request, parent),
        child_context,
        prepare_bundle(child_request, child),
        as_of=child.as_of,
        depth=1,
        budget=SearchBudget(20000),
        scales=settings.analysis.scales,
    )
    assert relations
    assert all(
        item.status in {"VERIFIED", "PROVISIONAL", "UNDETERMINED", "INCOMPATIBLE"}
        for item in relations
    )


@pytest.mark.parametrize(
    "gap,known_suspension,verified",
    [(False, False, True), (True, False, False), (True, True, True)],
)
def test_intraday_certification_requires_child_coverage(
    monkeypatch, gap, known_suspension, verified
):
    child = demo_bundle("AAPL", timeframe="5m")
    day = child.bars[len(child.bars) // 2].session_date
    left = next(bar for bar in reversed(child.bars) if bar.session_date < day)
    right = next(bar for bar in child.bars if bar.session_date > day)
    points = tuple(
        PivotPoint(
            pivot_id=bar.bar_id,
            index=0,
            kind=kind,
            price=price,
            extreme_at=bar.start_at,
            extreme_bar_id=bar.bar_id,
            state="CONFIRMED",
            confirmed_at=bar.usable_at,
            scale=1.0,
            threshold_at_extreme=1.0,
        )
        for bar, kind, price in ((left, "LOW", left.low), (right, "HIGH", right.high))
    )
    root = SimpleNamespace(
        node_id="root",
        pattern_type="IMPULSE",
        variant=None,
        points=tuple(
            SimpleNamespace(pivot=point, point_id=str(i)) for i, point in enumerate(points)
        ),
        child_links=(),
    )
    parent_scenario = SimpleNamespace(
        scenario_id="parent", structure=SimpleNamespace(root_id="root", nodes=(root,))
    )
    parent_context = SimpleNamespace(qualified=(parent_scenario,))
    child_context = SimpleNamespace(qualified=(SimpleNamespace(scenario_id="child"),))
    if gap:
        # Drop a full scheduled session while keeping its original expected slots.
        child = child.model_copy(
            update={
                "bars": tuple(bar for bar in child.bars if bar.session_date != day),
                "raw_bars": tuple(bar for bar in child.raw_bars if bar.session_date != day),
                "provenance": child.provenance.model_copy(
                    update={"known_suspensions": (day,) if known_suspension else ()}
                ),
            }
        )
    certified = WaveStructure(root_id="expected", status="VERIFIED", verified_depth=1, nodes=())
    monkeypatch.setattr(
        intraday_link_module,
        "match_interval",
        lambda *args, **kwargs: IntervalMatch("VERIFIED", "IMPULSE", points, structure=certified),
    )
    (result,) = link_intraday_timeframes(
        parent_context,
        child,
        child_context,
        child,
        as_of=child.as_of,
        depth=1,
        budget=SearchBudget(20),
        scales=(1.0,),
    )
    assert result.status == ("VERIFIED" if verified else "UNDETERMINED")
    if not verified:
        assert result.structure is None
        assert "INCOMPLETE_DATA" in result.reason


def test_daily_endpoint_maps_to_shortened_session_only_with_matching_complete_prices():
    fixture = demo_bundle("AAPL", timeframe="60m")
    day = date(2024, 7, 3)
    session = load_calendar(fixture.instrument, day, day).session(day)
    starts = tuple(intraday_windows(session, 60))
    bars = tuple(
        fixture.bars[index].model_copy(
            update=dict(
                start_at=opened,
                end_at=closed,
                available_at=closed,
                session_date=day,
                open=100.0,
                close=100.0,
                high=101.0 + index,
                low=99.0,
            )
        )
        for index, (opened, closed) in enumerate(starts)
    )
    assert bars[-1].end_at - bars[-1].start_at == timedelta(minutes=30)
    opened, closed, available = bar_window(session, "US")
    hourly = fixture.model_copy(
        update=dict(
            bars=bars,
            raw_bars=bars,
            sessions=(day,),
            as_of=available,
            expected_bar_starts=tuple(bar.start_at for bar in bars),
            provenance=fixture.provenance.model_copy(update={"basis_date": day}),
        )
    )
    native = bars[-1].model_copy(
        update=dict(start_at=opened, end_at=closed, available_at=available)
    )
    daily = hourly.model_copy(
        update=dict(timeframe="1d", bars=(native,), raw_bars=(native,), expected_bar_starts=())
    )
    point = PivotPoint(
        pivot_id="daily-high",
        index=0,
        extreme_bar_id=native.bar_id,
        extreme_at=native.start_at,
        confirmed_at=available,
        price=native.high,
        kind="HIGH",
        state="CONFIRMED",
        scale=1.0,
        threshold_at_extreme=1.0,
    )
    assert intraday_link_module._daily_endpoints(daily, hourly)(point) == len(bars) - 1

    partial = (bars[0], *bars[2:])
    broken = hourly.model_copy(update=dict(bars=partial, raw_bars=partial))
    assert intraday_link_module._daily_endpoints(daily, broken)(point) is None
    different = point.model_copy(update={"price": native.high + 0.01})
    assert intraday_link_module._daily_endpoints(daily, hourly)(different) is None


def test_intraday_replay_uses_only_bars_available_at_each_prefix():
    bundle = demo_bundle("AAPL", timeframe="5m")
    results = list(replay(bundle, AnalysisService(Settings()), step=50))
    assert len(results) >= 3
    usable = {bar.usable_at for bar in bundle.bars}
    for result in results:
        assert result.timeframe == "5m"
        assert result.as_of in usable
    assert results[0].as_of < results[-1].as_of


def _condition(kind, side, op, price):
    return LevelCondition(
        kind=kind,
        side=side,
        op=op,
        price=price,
        active_from=datetime(2024, 1, 5, 14, 30, tzinfo=ZoneInfo("America/New_York")),
        reason="fixture",
        coordinate_id="fixture",
    )


def _intraday_case():
    return OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        conditions=(
            OutlookCondition(
                role="ADVANCE",
                condition=_condition("CONFIRMATION", "HIGH", ">", 105),
            ),
            OutlookCondition(
                role="INVALIDATION",
                condition=_condition("INVALIDATION", "LOW", "<", 98),
            ),
        ),
        target=PriceZone(
            low=110,
            high=112,
            label="目标区1",
            methods=("fixture",),
            independent_method_count=1,
        ),
        target_mode="AFTER_ADVANCE",
        target_direction="UP",
    )


def _intraday_bar(index, close, high, low):
    start = datetime(2024, 1, 5, 19, 30, tzinfo=UTC) + timedelta(minutes=5 * index)
    return Bar(
        start_at=start,
        end_at=start + timedelta(minutes=5),
        session_date=date(2024, 1, 5),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        open=close,
        close=close,
        high=high,
        low=low,
        volume=100,
    )


def test_intraday_event_same_bar_target_and_stop_is_ambiguous():
    first = _intraday_bar(0, 104, 108, 99)
    second = _intraday_bar(1, 109, 111, 97)
    window = OutcomeWindow(
        (first.session_date, second.session_date),
        (first, second),
        second.session_date,
        period_starts=(first.start_at, second.start_at),
        period_ends=(first.end_at, second.end_at),
        observed_at=second.usable_at,
    )
    result = evaluate_outlook_event(
        EVENT_SPECS["OUTLOOK_TARGET_FIRST_5M_5B_V1"], _intraday_case(), window
    )
    assert result.status == "AMBIGUOUS_SAME_BAR"
    assert result.bar_id == second.bar_id
    assert result.start_at == second.start_at


def test_intraday_event_missing_first_bar_is_incomplete():
    first = _intraday_bar(0, 104, 108, 99)
    window = OutcomeWindow(
        (first.session_date,),
        (None,),
        first.session_date,
        period_starts=(first.start_at,),
        period_ends=(first.end_at,),
        observed_at=first.usable_at,
        incomplete_indices=frozenset({0}),
    )
    result = evaluate_outlook_event(EVENT_SPECS["ADVANCE_FIRST_5M_5B_V1"], _intraday_case(), window)
    assert result.status == "INCOMPLETE_DATA"


@pytest.mark.parametrize("later_bar", [False, True])
def test_intraday_missing_interval_uses_observed_time_not_session(later_bar):
    previous = _intraday_bar(0, 104, 105, 99)
    first = _intraday_bar(1, 104, 105, 99)
    later = _intraday_bar(2, 108, 111, 99)
    bars = (previous, later) if later_bar else (previous,)
    bundle = demo_bundle("AAPL", timeframe="5m").model_copy(
        update={"bars": bars, "raw_bars": bars, "as_of": bars[-1].usable_at}
    )
    period = OutlookPeriod(
        start_at=first.start_at,
        end_at=first.end_at,
        end_session=first.session_date,
        sessions=(first.session_date,),
    )
    window = intraday_outlook_window(bundle, first.session_date, (period,), 5)
    outcome = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5M_5B_V1"], _intraday_case(), window
    )
    assert outcome.status == ("INCOMPLETE_DATA" if later_bar else "RIGHT_CENSORED")


def test_intraday_bridge_keeps_trigger_source_bar_id():
    source_bar = _intraday_bar(1, 52, 55, 49)
    later = _intraday_bar(2, 53, 56, 51)
    fixture = demo_bundle("AAPL", timeframe="5m")
    bundle = fixture.model_copy(
        update={
            "bars": (source_bar, later),
            "raw_bars": (source_bar, later),
            "as_of": later.usable_at,
        }
    )
    periods = tuple(
        OutlookPeriod(
            start_at=bar.start_at,
            end_at=bar.end_at,
            end_session=bar.session_date,
            sessions=(bar.session_date,),
        )
        for bar in bundle.bars
    )
    window = intraday_outlook_window(bundle, source_bar.session_date, periods, 5, price_scale=2)
    outcome = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5M_5B_V1"], _intraday_case(), window
    )
    assert outcome.status == "CONFIRMED_FIRST"
    assert outcome.bar_id == source_bar.bar_id
    (observed,) = window.observed_bars(outcome)
    assert observed.high == 110 and observed.bar_id != outcome.bar_id


@pytest.mark.parametrize(
    "low,has_issuance_factor,trigger",
    [(51.0, True, False), (44.0, True, True), (44.0, False, False)],
)
def test_intraday_history_and_settlement_share_issuance_split_basis(
    low, has_issuance_factor, trigger
):
    fixture, run = _run("600519.SH", "5m")
    first_day, split_day = date(2024, 6, 3), date(2024, 6, 4)
    old_start = datetime(2024, 6, 3, 1, 30, tzinfo=UTC)
    new_start = old_start + timedelta(days=1)
    old_bar = _history_bar(old_start, first_day, 99.0)
    new_bar = _history_bar(new_start, split_day, low)
    old_factor = FactorPoint(effective_date=first_day, factor=1.0)
    factors = (old_factor, FactorPoint(effective_date=split_day, factor=2.0))
    issued = (old_factor,) if has_issuance_factor else ()
    old = _history_source(fixture, (old_bar,), factors=issued, qfq=True)
    new = _history_source(fixture, (old_bar, new_bar), factors=factors, qfq=True)
    previous, current = _history_briefs(run.brief, old, new, old_bar.end_at)

    comparison = compare_decisions(previous, current, previous_source=old, current_source=new)
    events = [event for event in comparison.events if event["type"] == "condition"]
    assert (comparison.change == "HARD_INVALIDATED") is trigger
    period = OutlookPeriod(
        start_at=new_bar.start_at,
        end_at=new_bar.end_at,
        end_session=split_day,
        sessions=(split_day,),
    )
    outcome = intraday_outlook_window(new, first_day, (period,), 1, issuance_factors=issued)
    if has_issuance_factor:
        assert outcome.bars[0].low == 2.0 * low
    else:
        assert outcome.price_basis_unverified
        assert comparison.change == "NOT_COMPARABLE"
        assert "复权因子" in comparison.reason
    if trigger:
        (event,) = events
        assert event["observed"] == 88.0
        assert event["bar_id"] == new_bar.bar_id
    else:
        assert not events


def _history_bar(start, day, low):
    return Bar(
        start_at=start,
        end_at=start + timedelta(minutes=5),
        session_date=day,
        observed_at=start + timedelta(minutes=5),
        open=low + 1.0,
        close=low + 1.0,
        high=low + 2.0,
        low=low,
        volume=100.0,
    )


def _history_source(fixture, bars, *, factors=(), qfq=False, expected=None):
    return fixture.model_copy(
        update={
            "bars": adjust_to_basis(bars, factors) if factors else bars,
            "raw_bars": bars,
            "as_of": bars[-1].usable_at,
            "expected_bar_starts": expected or tuple(bar.start_at for bar in bars),
            "sessions": tuple(sorted({bar.session_date for bar in bars})),
            "factors": factors,
            "provenance": fixture.provenance.model_copy(
                update={
                    "basis_date": bars[-1].session_date,
                    "adjustment": "basis_date_qfq" if qfq else "none",
                }
            ),
        }
    )


def _history_briefs(template, old, new, issued_at):
    first = old.bars[-1]
    condition = LevelCondition(
        kind="INVALIDATION",
        side="LOW",
        op="<",
        price=90.0,
        active_from=first.end_at,
        reason="threshold",
        coordinate_id=template.price_coordinate_id,
    )
    previous = template.model_copy(
        update={
            "as_of": first.usable_at,
            "market_asof": first.session_date,
            "last_bar_end_at": first.end_at,
            "adjustment": old.provenance.adjustment,
            "cards": (template.cards[0].model_copy(update={"conditions": (condition,)}),),
            "outlook": template.outlook.model_copy(update={"issued_at": issued_at}),
        }
    )
    last = new.bars[-1]
    return previous, previous.model_copy(
        update={
            "as_of": last.usable_at,
            "market_asof": last.session_date,
            "last_bar_end_at": last.end_at,
        }
    )


@pytest.mark.parametrize("missing_first", [False, True])
def test_intraday_history_checks_issuance_and_first_eligible_gap(missing_first):
    fixture, run = _run("600519.SH", "5m")
    day = date(2024, 6, 3)
    start = datetime(2024, 6, 3, 1, 30, tzinfo=UTC)
    old_bar = _history_bar(start, day, 99.0)
    first = _history_bar(start + timedelta(minutes=5), day, 51.0)
    last = _history_bar(start + timedelta(minutes=10), day, 51.0)
    expected = (old_bar.start_at, first.start_at, last.start_at)
    old = _history_source(fixture, (old_bar,))
    new = _history_source(
        fixture, (old_bar, last) if missing_first else (old_bar, first, last), expected=expected
    )
    issued_at = first.start_at if missing_first else last.start_at + timedelta(seconds=10)
    previous, current = _history_briefs(run.brief, old, new, issued_at)
    comparison = compare_decisions(previous, current, previous_source=old, current_source=new)
    events = [event for event in comparison.events if event["type"] == "condition"]
    assert comparison.change == "SAME_COUNT"
    assert [event["status"] for event in events] == (
        ["OBSERVED_AFTER_GAP"] if missing_first else []
    )


def test_live_intraday_outlook_excludes_slots_started_during_forecast(monkeypatch):
    bundle = demo_bundle("AAPL", timeframe="5m")
    boundary = bundle.bars[-1].end_at + timedelta(minutes=5)
    late = boundary + timedelta(seconds=1)
    now = boundary - timedelta(seconds=1)
    regular = brief_module.forecasts_for_brief

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def cross_boundary(*args, **kwargs):
        nonlocal now
        now = late
        return regular(*args, **kwargs)

    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(brief_module, "forecasts_for_brief", cross_boundary)
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, timeframe="5m", mode="LIVE")
    brief = build_brief(request, bundle, Settings(), check_windows=False).brief
    assert brief.generated_at == brief.outlook.issued_at == late
    assert brief.outlook.periods[0].start_at == boundary + timedelta(minutes=5)
    replay = build_brief(
        request, bundle, Settings(), check_windows=False, intraday_issued_at=boundary
    ).brief
    assert replay.outlook.issued_at == boundary
    assert replay.outlook.periods[0].start_at == boundary


def test_intraday_protocol_writes_bar_level_decisions_and_5b_events(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    cache = tmp_path / ".cache/elliottwaver"
    snapshot = SnapshotStore(cache).save(bundle)
    protocol = {
        "protocol_version": "1",
        "protocol_id": "intraday-test",
        "cache_root": str(cache),
        "samples": [
            {
                "symbol": "600519.SH",
                "asset_type": "EQUITY",
                "source_timeframe": "60m",
                "snapshot_id": snapshot,
            }
        ],
        "events": [
            "ADVANCE_FIRST_60M_5B_V1",
            "OUTLOOK_TARGET_FIRST_60M_5B_V1",
        ],
        "grid": {
            "start": "2024-01-10",
            "end": "2024-04-01",
            "cadence": "EVERY_BAR",
            "step": 20,
            "outcome_as_of": "2024-04-09",
        },
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    summary = evaluate_protocol(path, tmp_path / "evaluation")
    decisions = [
        json.loads(line)
        for line in (tmp_path / "evaluation" / "decisions.jsonl").read_text().splitlines()
    ]
    assert decisions
    assert all(item["timeframe"] == "60m" for item in decisions)
    assert len({item["decision_id"] for item in decisions}) == len(decisions)
    assert all(item["decision_at"] for item in decisions)
    assert summary["decisions"] > 0
    assert summary["events"]
    events = pq.read_table(tmp_path / "evaluation" / "events.parquet").to_pylist()
    assert events
    assert {row["event_id"] for row in events} == set(protocol["events"])
    assert all(row["due_at"] for row in events)
    assert all(row["period_starts"] for row in events)


@pytest.mark.parametrize(
    "symbol,timeframe,step,feed,adjustment",
    [
        ("600519.SH", "60m", 2, "synthetic", "synthetic_no_actions"),
        ("AAPL", "5m", 20, "sip", "provider_all"),
    ],
)
def test_forward_evaluation_uses_same_day_live_receipts(
    tmp_path, symbol, timeframe, step, feed, adjustment
):
    bundle = demo_bundle(symbol, timeframe=timeframe)
    bundle = bundle.model_copy(
        update={
            "provenance": bundle.provenance.model_copy(
                update={"feed": feed, "adjustment": adjustment}
            )
        }
    )
    cache = tmp_path / "cache"
    snapshot = SnapshotStore(cache).save(bundle)
    settings = Settings(cache={"directory": cache})
    day = bundle.bars[-1].session_date
    bars = [bar for bar in bundle.bars if bar.session_date == day][::step]
    assert len(bars) >= 2
    reports = tmp_path / "reports"
    issued = []
    for bar in bars:
        request = AnalysisRequest(
            symbol=symbol, as_of=bar.usable_at, timeframe=timeframe, mode="LIVE"
        )
        run = build_brief(
            request,
            bundle,
            settings,
            check_windows=False,
            intraday_issued_at=bar.usable_at + timedelta(minutes=1),
        )
        publish_brief(run, reports, formats=())
        issued.append(run.brief)
    protocol = {
        "protocol_version": "1",
        "protocol_id": "intraday-live-receipts",
        "data_status": "SEALED_FORWARD_HOLDOUT",
        "cache_root": str(cache),
        "samples": [
            {
                "symbol": symbol,
                "asset_type": "EQUITY",
                "source_timeframe": timeframe,
                "snapshot_id": snapshot,
            }
        ],
        "events": [f"ADVANCE_FIRST_{timeframe.upper()}_5B_V1"],
        "grid": {
            "start": day.isoformat(),
            "end": day.isoformat(),
            "cadence": "EVERY_BAR",
            "step": step,
        },
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    out = tmp_path / "eval"
    evaluate_protocol(path, out, issued_reports=reports)
    rows = [json.loads(line) for line in (out / "decisions.jsonl").read_text().splitlines()]
    assert [row["issued_brief_digest"] for row in rows] == [brief.brief_digest for brief in issued]
    assert [row["last_bar_end_at"] for row in rows] == [bar.end_at.isoformat() for bar in bars]
    assert [row["outlook"] for row in rows] == [b.outlook.model_dump(mode="json") for b in issued]
    assert [row["decision_at"] for row in rows] == [bar.start_at.isoformat() for bar in bars]
    assert pq.read_table(out / "events.parquet").num_rows > 0


def test_same_day_intraday_history_orders_by_last_bar_not_insertion(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    runs = []
    for decision in (bundle.bars[-30], bundle.bars[-10]):
        runs.append(
            build_brief(
                AnalysisRequest(
                    symbol="600519.SH",
                    as_of=decision.usable_at,
                    end=decision.session_date,
                    timeframe="60m",
                    mode="FROZEN_DATA_REPLAY",
                ),
                bundle,
                Settings(),
                include_weekly=False,
                check_windows=False,
            )
        )
    store = HistoryStore(tmp_path / ".cache/elliottwaver")
    paths = []
    for index, run in enumerate(runs):
        paths.append(publish_brief(run, tmp_path / f"reports-{index}", formats=("md",)))
    # Write the later decision first, then the earlier decision.
    store.register(runs[1].brief, paths[1])
    store.register(runs[0].brief, paths[0])
    rows = store.query("600519.SH", timeframe="60m")
    assert [row["brief_digest"] for row in rows] == [
        runs[1].brief.brief_digest,
        runs[0].brief.brief_digest,
    ]
    previous = store.previous(runs[1].brief)
    assert previous is not None
    assert previous["brief_digest"] == runs[0].brief.brief_digest
    assert previous["last_bar_end_at"] == runs[0].brief.last_bar_end_at.isoformat()


def test_intraday_snapshot_is_adopted_by_auto_brief_timeframe():
    bundle = demo_bundle("600519.SH", timeframe="60m")
    chosen, reason = _choose_brief_timeframe("auto", bundle, None, bundle.as_of)
    assert chosen == "60m"
    assert "分钟" in reason


def test_intraday_brief_cli_auto_and_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = demo_bundle("600519.SH", timeframe="60m")
    snapshot = SnapshotStore(tmp_path / ".cache/elliottwaver").save(bundle)
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "600519.SH",
            "--offline",
            "--snapshot",
            snapshot,
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["timeframe"] == "60m"
    assert payload["outlook"]["horizon_unit"] == "BARS"
    assert payload["outlook"]["horizon"] == 5


def test_intraday_settlement_uses_frozen_exact_window(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    decision = bundle.bars[-10]
    run = build_brief(
        AnalysisRequest(
            symbol="600519.SH",
            as_of=decision.usable_at,
            end=decision.session_date,
            timeframe="60m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    assert run.brief.outlook is not None
    assert run.brief.outlook.horizon_unit == "BARS"
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    inserted = store.register_brief(run.brief, path)
    assert inserted >= 1
    store.settle_brief(run.brief, path, bundle)
    rows = store.rows(brief_digest=run.brief.brief_digest)
    assert rows
    assert all(row["timeframe"] == "60m" for row in rows)
    assert all(row["horizon_unit"] == "BARS" for row in rows)
    assert any(row["event_id"].startswith("ADVANCE_FIRST_60M_") for row in rows)
    assert any(row["event_id"].startswith("OUTLOOK_TARGET_FIRST_60M_") for row in rows)


def test_intraday_settlement_rejects_old_weekly_window(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    decision = bundle.bars[-10]
    run = build_brief(
        AnalysisRequest(
            symbol="600519.SH",
            as_of=decision.usable_at,
            end=decision.session_date,
            timeframe="60m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    old = run.brief.outlook.model_copy(
        update={
            "version": "1.0",
            "horizon": 2,
            "horizon_unit": "WEEKS",
            "periods": run.brief.outlook.periods[:1],
        }
    )
    brief = run.brief.model_copy(update={"outlook": old})
    with pytest.raises(Exception, match="旧观察窗与主周期不匹配"):
        event_specs(brief)
