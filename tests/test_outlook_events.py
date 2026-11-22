from datetime import UTC, date, datetime

from conftest import make_bar

from elliottwaver.evaluation.events import EVENT_SPECS
from elliottwaver.evaluation.outcomes import OutcomeWindow, evaluate_outlook_event
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
from elliottwaver.schemas.wave import LevelCondition, PriceZone

_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def _condition(kind, side, op, price, reason="fixture"):
    return LevelCondition(
        kind=kind,
        side=side,
        op=op,
        price=price,
        active_from=_AS_OF,
        reason=reason,
        coordinate_id="fixture",
    )


def _case(*, advance=True, target_mode="AFTER_ADVANCE", target=True):
    conditions = [
        OutlookCondition(
            role="INVALIDATION",
            condition=_condition("INVALIDATION", "LOW", "<=", 130),
            source_index=0,
        )
    ]
    if advance:
        conditions.insert(
            0,
            OutlookCondition(
                role="ADVANCE",
                condition=_condition("CONFIRMATION", "HIGH", ">", 150),
                source_index=1,
            ),
        )
    return OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        conditions=tuple(conditions),
        target=PriceZone(
            low=156,
            high=162,
            label="目标区1",
            methods=("fixture",),
            independent_method_count=1,
        )
        if target
        else None,
        target_mode=target_mode if target else "NONE",
        target_direction="UP",
    )


def _window(rows):
    bars = tuple(
        make_bar(index, close, high=high, low=low) for index, (close, high, low) in enumerate(rows)
    )
    return OutcomeWindow(
        tuple(bar.session_date for bar in bars),
        bars,
        bars[-1].session_date,
    )


def test_outlook_advance_event_confirms_before_stop():
    result = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5D_V1"],
        _case(),
        _window([(150, 151, 140), (150, 150, 140)]),
    )
    assert result.status == "CONFIRMED_FIRST"


def test_outlook_advance_event_same_bar_is_ambiguous():
    result = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5D_V1"],
        _case(),
        _window([(140, 151, 129)]),
    )
    assert result.status == "AMBIGUOUS_SAME_BAR"


def test_outlook_advance_event_stops_first():
    result = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5D_V1"],
        _case(),
        _window([(140, 149, 129)]),
    )
    assert result.status == "INVALIDATED_FIRST"


def test_outlook_target_waits_for_advance_then_hits():
    result = evaluate_outlook_event(
        EVENT_SPECS["OUTLOOK_TARGET_FIRST_5D_V1"],
        _case(),
        _window([(150, 151, 140), (157, 157, 150)]),
    )
    assert result.status == "TARGET_FIRST"
    assert result.session == date(2024, 1, 2)


def test_outlook_target_and_advance_same_bar_is_ambiguous():
    result = evaluate_outlook_event(
        EVENT_SPECS["OUTLOOK_TARGET_FIRST_5D_V1"],
        _case(),
        _window([(157, 157, 150)]),
    )
    assert result.status == "AMBIGUOUS_SAME_BAR"


def test_outlook_active_target_hits_immediately():
    result = evaluate_outlook_event(
        EVENT_SPECS["OUTLOOK_TARGET_FIRST_5D_V1"],
        _case(target_mode="ACTIVE"),
        _window([(157, 157, 150)]),
    )
    assert result.status == "TARGET_FIRST"


def test_outlook_event_expires_without_trigger():
    result = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5D_V1"],
        _case(),
        _window([(140, 149, 131)] * 5),
    )
    assert result.status == "EXPIRED"


def test_outlook_event_without_advance_gate_is_undefined():
    result = evaluate_outlook_event(
        EVENT_SPECS["ADVANCE_FIRST_5D_V1"],
        _case(advance=False),
        _window([(140, 149, 131)] * 5),
    )
    assert result.status == "UNDEFINED_EVENT"
