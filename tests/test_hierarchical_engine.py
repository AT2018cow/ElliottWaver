from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import make_pivots

from eliotbot.config import Settings
from eliotbot.data.demo import demo_bundle
from eliotbot.data.router import route_symbol
from eliotbot.engine.hierarchical import build_structure, match_interval
from eliotbot.engine.matcher import Candidate, SearchBudget
from eliotbot.engine.rules import classify_variant
from eliotbot.engine.templates import IMPULSE
from eliotbot.engine.timeframe_link import link_timeframes
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import AnalysisRequest, Bar, MarketDataBundle, Provenance
from eliotbot.schemas.wave import PivotPoint, WaveLabel, WaveScenario
from eliotbot.service import AnalysisService
from eliotbot.util import digest


def _as_of():
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_match_interval_certifies_complete_impulse():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    result = match_interval(
        points[0],
        points[5],
        ("IMPULSE",),
        bars,
        [ZigZagResult(points[1:5], None)],
        as_of=_as_of(),
        depth=1,
        budget=SearchBudget(100),
    )
    assert result.status == "VERIFIED"
    assert result.template == "IMPULSE"
    assert len(result.points) == 6
    assert not result.failed_rule_ids


@pytest.mark.parametrize("reverse", [False, True])
def test_interval_search_tries_other_counts_when_first_match_has_no_subdivision(reverse):
    prices = [100, 110, 105, 124, 116, 130, 120, 126, 117, 128, 122, 143, 134, 150]
    bars, points = make_pivots(prices)
    left, right = (points[i].model_copy(update={"scale": 3.0}) for i in (0, 13))
    fine = ZigZagResult(tuple(p.model_copy(update={"scale": 1.0}) for p in points[1:-1]), None)
    coarse = ZigZagResult(
        tuple(points[i].model_copy(update={"scale": 2.0}) for i in (3, 4, 5, 8)), None
    )
    scales = (coarse, fine) if reverse else (fine, coarse)
    match = match_interval(
        left,
        right,
        ("ZIGZAG_ABC",),
        bars,
        scales,
        as_of=_as_of(),
        depth=2,
        budget=SearchBudget(1000),
    )
    assert match.scale == 2.0
    assert [p.index for p in match.points] == [0, 5, 8, 13]
    assert match.verified_layers == 2
    assert match.structure is not None and match.structure.status == "VERIFIED"


@pytest.mark.parametrize("w_leg", [(117, 100.5), (120, 110)])
def test_nested_wxy_requires_the_subdivision_of_the_matching_variant(w_leg):
    prices = [100, *w_leg, 130, 115, 125, 110, 116, 113, 118]
    bars, points = make_pivots(prices)
    left, right = (points[i].model_copy(update={"scale": 3.0}) for i in (0, 9))
    scales = (
        ZigZagResult(tuple(points[i].model_copy(update={"scale": 2.0}) for i in (3, 6)), None),
        ZigZagResult(tuple(p.model_copy(update={"scale": 1.0}) for p in points[1:-1]), None),
    )
    match = match_interval(
        left,
        right,
        ("WXY",),
        bars,
        scales,
        as_of=_as_of(),
        depth=1,
        budget=SearchBudget(1000),
    )
    if w_leg == (117, 100.5):
        assert (match.status, match.variant) == ("VERIFIED", "DOUBLE_THREE")
        assert [p.index for p in match.points] == [0, 3, 6, 9]
        assert [n.pattern_type for n in match.structure.nodes] == [
            "WXY",
            "FLAT",
            "ZIGZAG_ABC",
            "ZIGZAG_ABC",
        ]
    else:
        assert match.status != "VERIFIED"


def test_match_interval_rejects_abc_inside_impulse():
    bars, points = make_pivots([100, 120, 110, 115, 105, 130])
    result = match_interval(
        points[0],
        points[5],
        ("IMPULSE",),
        bars,
        [ZigZagResult(points[1:5], None)],
        as_of=_as_of(),
        depth=1,
        budget=SearchBudget(100),
    )
    assert result.status == "REJECTED"
    assert result.failed_rule_ids


def test_match_interval_reports_budget_exhaustion():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    result = match_interval(
        points[0],
        points[5],
        ("IMPULSE",),
        bars,
        [ZigZagResult(points[1:5], None)],
        as_of=_as_of(),
        depth=1,
        budget=SearchBudget(0),
    )
    assert result.status == "SEARCH_LIMITED"
    assert result.reason_code == "BUDGET_EXHAUSTED"


def test_match_interval_checks_boundary_and_interior_confirmation_time():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160])

    def match(pivots, cutoff):
        return match_interval(
            pivots[0],
            pivots[-1],
            ("IMPULSE",),
            bars,
            [ZigZagResult(pivots[1:-1], None)],
            as_of=cutoff,
            depth=1,
            budget=SearchBudget(100),
        )

    assert match(points, bars[-2].usable_at).status == "PENDING"
    unfinished = points[2].model_copy(update={"state": "DEVELOPING", "confirmed_at": None})
    assert match((*points[:2], unfinished, *points[3:]), bars[-1].usable_at).status == "PENDING"
    seeded = points[0].model_copy(update={"state": "SEED"})
    assert match((seeded, *points[1:]), bars[-1].usable_at).status == "PENDING"
    assert match(points, bars[-1].usable_at).status == "VERIFIED"


def test_build_structure_records_legs_and_child_nodes():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    candidate = Candidate(IMPULSE, points, points[-1], 1, "COMPLETE", (), (1.4,))
    structure = build_structure(
        candidate,
        bars,
        [
            ZigZagResult(
                tuple(point.model_copy(update={"scale": 0.7}) for point in points[1:5]),
                None,
            )
        ],
        as_of=_as_of(),
        coordinate_id="fixture",
        depth=1,
        budget=SearchBudget(100),
        timeframe="1d",
    )
    assert structure.status in {"PENDING", "VERIFIED"}
    root = structure.nodes[0]
    assert len(root.child_links) == 5
    assert all(leg.status in {"PENDING", "VERIFIED"} for leg in root.child_links)


def test_hierarchical_profile_sets_schema_and_structure():
    bundle = demo_bundle("AAPL")
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1", "scales": (0.7, 1.4, 2.4, 3.8)},
        search={"max_subwave_depth": 1},
    )
    result = AnalysisService(settings).analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    assert result.schema_version == "1.1"
    assert result.engine_version.endswith("/hierarchical_v1")
    assert result.scenarios
    assert all(scenario.structure is not None for scenario in result.scenarios)
    assert any(scenario.structure.nodes[0].child_links for scenario in result.scenarios)


def _link_contexts(
    *,
    fixed_weekly=True,
    variant_pattern="IMPULSE",
    gap=False,
    equal_extreme=False,
    adjusted=False,
    raw_revision=False,
    feed="sip",
    daily_proof=True,
    two_parents=False,
    historical=False,
    depth=1,
):
    from eliotbot.data.periods import as_weekly
    from eliotbot.engine.templates import TEMPLATES
    from eliotbot.schemas.wave import WaveStructure

    prices = [
        110,
        108,
        107,
        105,
        100,
        113,
        108,
        135,
        125,
        150,
        143,
        147,
        139,
        149,
        130,
        136,
        142,
        154,
        149,
        160,
        150,
        141,
        146,
        140,
        137,
        146,
        149,
        152,
        157,
        155,
    ]
    if equal_extreme:
        prices[8] = prices[9]
    bars = []
    for index, price in enumerate(prices):
        day = date(2024, 1, 8) + timedelta(days=(index // 5) * 7 + index % 5)
        start = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        bars.append(
            Bar(
                start_at=start,
                end_at=start + timedelta(hours=6, minutes=30),
                observed_at=_as_of(),
                session_date=day,
                volume=100,
                open=price,
                high=price,
                low=price,
                close=price,
            )
        )
    raw = tuple(bars)
    instrument = route_symbol("AAPL")
    provenance = Provenance(
        provider="fixture",
        feed="sip",
        adjustment="provider_all",
        price_coordinate_id="original",
        basis_date=raw[-1].session_date,
        session_profile="fixture",
        calendar_version="fixture",
        fetched_at=_as_of(),
        requested_start=raw[0].session_date,
        requested_end=raw[-1].session_date,
        amount_currency="USD",
    )
    source = MarketDataBundle(
        instrument=instrument,
        as_of=raw[-1].usable_at,
        bars=raw,
        raw_bars=raw,
        provenance=provenance,
        sessions=tuple(bar.session_date for bar in raw),
    )
    weekly = as_weekly(source)
    raw_daily = tuple(bar for i, bar in enumerate(raw) if not gap or i != 6)
    daily_bars = raw_daily
    if adjusted or raw_revision:
        daily_bars = tuple(
            Bar.model_validate(
                {
                    **bar.model_dump(),
                    **{name: getattr(bar, name) / 2 for name in ("open", "high", "low", "close")},
                }
            )
            for bar in raw_daily
        )
        if raw_revision:
            raw_daily = daily_bars
    daily = MarketDataBundle(
        instrument=instrument,
        as_of=source.as_of,
        bars=daily_bars,
        raw_bars=raw_daily,
        provenance=provenance.model_copy(
            update={
                "feed": feed,
                "price_coordinate_id": "updated" if adjusted or raw_revision else "original",
            }
        ),
        sessions=source.sessions,
    )

    def points(bundle, indices, first_low, other_tip=False):
        result = []
        for n, index in enumerate(indices):
            chosen = bundle.bars[index]
            kind = "LOW" if (n % 2 == 0) == first_low else "HIGH"
            tip = n == len(indices) - 1 and other_tip
            result.append(
                PivotPoint(
                    pivot_id=f"{bundle.timeframe}-{index}-{kind}",
                    index=index,
                    extreme_bar_id=chosen.bar_id,
                    extreme_at=chosen.start_at,
                    confirmed_at=None if tip else chosen.usable_at,
                    price=getattr(chosen, kind.lower()),
                    kind=kind,
                    state="DEVELOPING" if tip else "CONFIRMED",
                    scale=2.4 if bundle.timeframe == "1w" else 1.4,
                    threshold_at_extreme=1.0,
                )
            )
        return tuple(result)

    def scenario(pattern, pivots, stage="COMPLETE", verified=False):
        labels = next(template.labels for template in TEMPLATES if template.name == pattern)
        completed = pivots if stage == "COMPLETE" else pivots[:-1]
        identifier = digest([pattern, stage, pivots])[:24]
        # The matcher consumes the certificate status here. Child proof algorithms have
        # separate tests with real finer pivots.
        structure = (
            WaveStructure(
                root_id=identifier, status="VERIFIED", requested_depth=1, verified_depth=1, nodes=()
            )
            if verified
            else None
        )
        return WaveScenario.model_construct(
            scenario_id=identifier,
            pattern_type=pattern,
            stage=stage,
            completed_waves=tuple(
                WaveLabel(label=label, pivot=p) for label, p in zip(labels, completed, strict=False)
            ),
            developing_tip=pivots[-1],
            discovered_scales=(1.4,),
            variant=None,
            structure=structure,
            subwave_status="VERIFIED" if verified else "UNAVAILABLE",
        )

    upper_points = points(
        weekly,
        [0, 1, 2, 3, 4, 5] if fixed_weekly else [0, 1, 2, 3, 4],
        True,
        other_tip=not fixed_weekly,
    )
    parent = scenario("IMPULSE", upper_points, "COMPLETE" if fixed_weekly else "W4_DEVELOPING")
    parents = (parent,)
    if two_parents:
        parents += (parent.model_copy(update={"scenario_id": "second-parent"}),)
    if variant_pattern == "TRIANGLE":
        daily_indices = [9, 10, 11, 12, 13, 14]
    elif variant_pattern == "ZIGZAG_ABC":
        daily_indices = [19, 21, 22, 24]
    else:
        daily_indices = [4, 5, 6, 7, 8, 9] if not gap else [4, 5, 6, 7, 8]
    lower_points = points(daily, daily_indices, variant_pattern == "IMPULSE")
    child = scenario(variant_pattern, lower_points, verified=daily_proof)
    children = () if historical else (child,)
    context = SimpleNamespace(
        qualified=children,
        result=SimpleNamespace(scenarios=children),
        zigzags=(ZigZagResult(lower_points, None),),
    )
    upper = SimpleNamespace(qualified=parents, result=SimpleNamespace(scenarios=parents))
    return link_timeframes(
        context, daily, upper, weekly, as_of=source.as_of, depth=depth, budget=SearchBudget(1000)
    )


def test_link_timeframes_marks_exact_weekly_leg():
    relation = next(
        r for r in _link_contexts() if r.parent_wave_label == "1" and r.daily_scenario_id
    )
    assert (relation.status, relation.relation) == ("VERIFIED", "EXACT_LEG")
    assert len(relation.weekly_point_ids) == 2 and len(relation.daily_point_ids) == 6


def test_link_timeframes_downgrades_unverified_subwaves():
    assert any(
        r.parent_wave_label == "1" and r.status == "PROVISIONAL" and r.daily_scenario_id
        for r in _link_contexts(daily_proof=False)
    )


def test_link_timeframes_marks_incomparable_price_basis_undetermined():
    assert not any(r.status == "VERIFIED" for r in _link_contexts(raw_revision=True))
    assert not any(r.status == "VERIFIED" for r in _link_contexts(feed="iex"))


def test_link_timeframes_accepts_proven_split_adjustment():
    assert any(
        r.parent_wave_label == "1" and r.status == "VERIFIED" for r in _link_contexts(adjusted=True)
    )


def test_link_timeframes_links_two_parent_candidates():
    ids = {r.weekly_scenario_id for r in _link_contexts(two_parents=True) if r.status == "VERIFIED"}
    assert len(ids) == 2


def test_expensive_historical_link_keeps_the_other_displayed_relationships(monkeypatch):
    from eliotbot.engine.hierarchical import IntervalMatch

    allowances = []

    def exhaust(*args, budget, **kwargs):
        allowances.append(budget.limit)
        budget.expansions = budget.limit
        assert not budget.take()
        return IntervalMatch("SEARCH_LIMITED")

    monkeypatch.setattr("eliotbot.engine.timeframe_link.match_interval", exhaust)
    relations = _link_contexts(two_parents=True, daily_proof=False)
    displayed = {r.weekly_scenario_id for r in relations if r.daily_scenario_id}
    assert len(allowances) == 2 and len(displayed) == 2
    assert allowances[0] > 0 and allowances[1] > 0


def test_link_timeframes_marks_equal_extremes_provisional():
    assert any(
        r.parent_wave_label == "1" and r.status == "PROVISIONAL"
        for r in _link_contexts(equal_extreme=True)
    )


def test_link_timeframes_cannot_verify_a_missing_daily_session():
    assert not any(r.status == "VERIFIED" for r in _link_contexts(gap=True))


def test_link_timeframes_rejects_triangle_in_wave_two():
    assert any(
        r.parent_wave_label == "2" and r.status == "INCOMPATIBLE" and r.daily_scenario_id
        for r in _link_contexts(variant_pattern="TRIANGLE")
    )


def test_developing_wave_four_keeps_corrective_daily_count_provisional():
    assert any(
        r.parent_wave_label == "4" and r.status == "PROVISIONAL" and r.daily_scenario_id
        for r in _link_contexts(fixed_weekly=False, variant_pattern="ZIGZAG_ABC")
    )


def test_historical_endpoints_match_without_fabricating_subdivision_evidence():
    assert any(
        r.parent_wave_label == "1"
        and r.relation == "EXACT_LEG"
        and r.status == "PROVISIONAL"
        and r.daily_point_ids
        and r.daily_scenario_id is None
        for r in _link_contexts(historical=True)
    )


@pytest.mark.parametrize("depth", [0, 2])
def test_weekly_links_use_requested_depth_for_displayed_and_historical_counts(depth):
    displayed = next(
        r for r in _link_contexts(depth=depth) if r.parent_wave_label == "1" and r.daily_scenario_id
    )
    historical = next(
        r for r in _link_contexts(historical=True, depth=depth) if r.parent_wave_label == "1"
    )
    assert displayed.status == historical.status == "PROVISIONAL"
    if depth == 0:
        assert historical.structure is None and len(historical.daily_point_ids) == 2
    else:
        assert historical.structure.requested_depth == 2
        assert historical.structure.verified_depth == 0


def test_historical_weekly_link_with_one_proven_layer_is_only_verified_at_depth_one():
    from eliotbot.data.periods import as_weekly

    prices = [
        160,
        154,
        148,
        140,
        138,
        100,
        110,
        105,
        124,
        116,
        130,
        120,
        126,
        117,
        128,
        122,
        143,
        134,
        150,
        149,
        147,
        140,
        138,
        137,
        133,
    ]
    made_bars, made_points = make_pivots(prices)
    bars = []
    for index, bar in enumerate(made_bars):
        day = date(2024, 2, 5) + timedelta(days=7 * (index // 5) + index % 5)
        start = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        bars.append(
            bar.model_copy(
                update={
                    "session_date": day,
                    "start_at": start,
                    "end_at": start + timedelta(hours=6, minutes=30),
                    "observed_at": _as_of(),
                }
            )
        )
    daily = demo_bundle("AAPL").model_copy(
        update={
            "as_of": _as_of(),
            "bars": tuple(bars),
            "raw_bars": tuple(bars),
            "sessions": tuple(b.session_date for b in bars),
        }
    )
    weekly = as_weekly(daily)

    def point(bar, index, kind, scale):
        return made_points[0].model_copy(
            update={
                "index": index,
                "kind": kind,
                "scale": scale,
                "price": getattr(bar, kind.lower()),
                "pivot_id": f"{index}-{kind}-{scale}",
                "extreme_bar_id": bar.bar_id,
                "extreme_at": bar.start_at,
                "confirmed_at": bar.usable_at,
            }
        )

    fixed = tuple(
        point(weekly.bars[i], i, kind, 3.8) for i, kind in [(0, "HIGH"), (1, "LOW"), (3, "HIGH")]
    )
    tip = point(weekly.bars[4], 4, "LOW", 3.8).model_copy(
        update={"state": "DEVELOPING", "confirmed_at": None}
    )
    parent = WaveScenario.model_construct(
        scenario_id="weekly-parent",
        pattern_type="IMPULSE",
        stage="W3_DEVELOPING",
        completed_waves=tuple(WaveLabel(label=str(i), pivot=p) for i, p in enumerate(fixed)),
        developing_tip=tip,
        variant=None,
    )
    fine = tuple(
        point(bars[i], i, "LOW" if (i - 5) % 2 == 0 else "HIGH", 1.0) for i in range(5, 19)
    )
    coarse = tuple(fine[i - 5].model_copy(update={"scale": 2.4}) for i in (5, 10, 13, 18))
    lower = SimpleNamespace(
        qualified=(),
        result=SimpleNamespace(scenarios=()),
        zigzags=(ZigZagResult(fine, None), ZigZagResult(coarse, None)),
    )
    upper = SimpleNamespace(qualified=(parent,), result=SimpleNamespace(scenarios=(parent,)))
    for depth, expected in ((0, "PROVISIONAL"), (1, "VERIFIED"), (2, "PROVISIONAL")):
        relations = link_timeframes(lower, daily, upper, weekly, as_of=_as_of(), depth=depth)
        relation = next(r for r in relations if r.parent_wave_label == "2")
        assert relation.relation == "EXACT_LEG" and relation.status == expected
        if depth:
            assert relation.structure.requested_depth == depth
            assert relation.structure.verified_depth == 1
        else:
            assert relation.structure is None


def test_flat_variants_are_classified_from_endpoints():
    _, regular = make_pivots([100, 140, 101, 141])
    _, expanded = make_pivots([100, 140, 92, 149])
    _, running = make_pivots([100, 140, 92, 127])
    _, weak_running = make_pivots([100, 140, 99, 127])
    assert classify_variant("FLAT", regular, [], 1, 0.01) == "REGULAR_FLAT"
    assert classify_variant("FLAT", expanded, [], 1, 0.01) == "EXPANDED_FLAT"
    assert classify_variant("FLAT", running, [], 1, 0.01) == "RUNNING_FLAT"
    assert classify_variant("FLAT", weak_running, [], 1, 0.01) is None


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize(
    "prices, variant",
    [
        ([100, 140, 99.99, 141], "EXPANDED_FLAT"),
        ([100, 140, 101, 140.01], "REGULAR_FLAT"),
        ([100, 140, 100, 140.01], "REGULAR_FLAT"),
        ([100, 140, 99.99, 127], None),
    ],
)
def test_flat_subtypes_use_actual_origin_and_a_crossings(prices, variant, direction):
    _, points = make_pivots(prices if direction > 0 else [300 - p for p in prices])
    assert classify_variant("FLAT", points, [], direction, 0.01) == variant


def test_engine_profile_changes_config_identity_only_in_advanced_mode():
    classic = Settings()
    explicit = Settings(analysis={"engine_profile": "classic_v4"})
    advanced = Settings(analysis={"engine_profile": "hierarchical_v1"})
    assert classic.computation_hash == explicit.computation_hash
    assert advanced.computation_hash != classic.computation_hash


def test_near_identical_candidates_merge_scales_without_weight_inflation():
    from eliotbot.engine.matcher import deduplicate_candidates

    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    first = Candidate(IMPULSE, points, points[-1], 1, "COMPLETE", (), (1.4,))
    second = Candidate(IMPULSE, points, points[-1], 1, "COMPLETE", (), (2.4,))
    merged = deduplicate_candidates([first, second], tick_size=0.01)
    assert len(merged) == 1
    assert merged[0].scales == (1.4, 2.4)


def _daily_from_weekly_demo():
    from datetime import time
    from zoneinfo import ZoneInfo

    from eliotbot.data.demo import demo_bundle
    from eliotbot.schemas.market import MarketDataBundle, Provenance

    weekly = demo_bundle("AAPL", timeframe="1w")
    zone = ZoneInfo("America/New_York")
    bars = []
    sessions = []
    for week in weekly.bars:
        days = []
        day = week.session_date
        while len(days) < 5:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.sort()
        sessions.extend(days)
        for day in days:
            start = datetime.combine(day, time.min, zone).astimezone(UTC)
            end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
            bars.append(
                Bar(
                    start_at=start,
                    end_at=end,
                    available_at=end + timedelta(minutes=20),
                    session_date=day,
                    observed_at=_as_of(),
                    open=week.open,
                    high=week.high,
                    low=week.low,
                    close=week.close,
                    volume=week.volume / 5,
                )
            )
    provenance = Provenance(
        provider="alpaca",
        feed="sip",
        adjustment="provider_all",
        price_coordinate_id="fixture",
        basis_date=sessions[-1],
        session_profile="provider_native_daily",
        calendar_version="fixture",
        fetched_at=datetime.now(UTC),
        requested_start=sessions[0],
        requested_end=sessions[-1],
        amount_currency="USD",
    )
    return MarketDataBundle(
        instrument=route_symbol("AAPL").model_copy(update={"identity_verified": True}),
        as_of=bars[-1].usable_at,
        bars=tuple(bars),
        raw_bars=tuple(bars),
        provenance=provenance,
        sessions=tuple(sessions),
    )


def test_advanced_brief_links_daily_and_weekly_structures():
    from eliotbot.brief import build_brief

    source = _daily_from_weekly_demo()
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1", "scales": (0.7, 1.4, 2.4, 3.8)},
        search={"max_subwave_depth": 1},
    )
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=source.as_of),
        source,
        settings,
        include_weekly=True,
        check_windows=False,
    )
    assert run.brief.schema_version == "1.2"
    assert run.brief.weekly_state == "AVAILABLE"
    assert run.brief.wave_relations
    assert all(card.structure is not None for card in run.brief.cards)


def test_diagonal_variants_are_classified_from_contracting_rails():
    _, contracting = make_pivots([100, 150, 120, 160, 140, 155])
    _, expanding = make_pivots([100, 120, 110, 150, 130, 180])
    assert classify_variant("DIAGONAL", contracting, [], 1, 0.01) == "CONTRACTING_DIAGONAL"
    assert classify_variant("DIAGONAL", expanding, [], 1, 0.01) == "EXPANDING_DIAGONAL"


def test_barrier_triangle_endpoint_examples():
    _, barrier = make_pivots([100, 120, 105, 116, 105, 111])
    _, not_barrier = make_pivots([100, 120, 105, 120, 110, 115])
    assert classify_variant("TRIANGLE", barrier, [], 1, 0.01) == "BARRIER_TRIANGLE"
    assert classify_variant("TRIANGLE", not_barrier, [], 1, 0.01) is None


def test_truncated_fifth_requires_the_explicit_variant():
    from eliotbot.engine.rules import check_pattern

    bars, points = make_pivots([100, 125, 113, 152, 135, 149])
    regular = check_pattern("IMPULSE", points, bars, 1, developing=False)
    assert next(rule for rule in regular if rule.rule_id == "W5_PROGRESS").status == "FAIL"
    truncated = check_pattern(
        "IMPULSE", points, bars, 1, developing=False, variant="TRUNCATED_FIFTH"
    )
    assert not any(rule.status == "FAIL" for rule in truncated)
    assert next(rule for rule in truncated if rule.rule_id == "W5_PROGRESS").status == "PASS"


def test_advanced_diagonal_permissions_cover_33333_and_53535():
    from eliotbot.engine.templates import allowed_child_templates_advanced

    leading = allowed_child_templates_advanced("DIAGONAL", 0, variant="LEADING_DIAGONAL")
    assert "IMPULSE" in leading
    assert "DIAGONAL" in leading
    assert "ZIGZAG_ABC" in leading
    assert "IMPULSE" not in allowed_child_templates_advanced(
        "DIAGONAL", 1, variant="LEADING_DIAGONAL"
    )
    ending = allowed_child_templates_advanced("DIAGONAL", 0, variant="ENDING_DIAGONAL")
    assert ending == ("ZIGZAG_ABC", "FLAT", "WXY")
    assert allowed_child_templates_advanced("DIAGONAL", 0) == ending


def test_match_interval_certifies_leading_diagonal_53535_leg():
    from eliotbot.engine.templates import allowed_child_templates_advanced

    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    result = match_interval(
        points[0],
        points[5],
        allowed_child_templates_advanced("DIAGONAL", 0, variant="LEADING_DIAGONAL"),
        bars,
        [ZigZagResult(points[1:5], None)],
        as_of=_as_of(),
        depth=1,
        budget=SearchBudget(100),
    )
    assert result.status == "VERIFIED"
    assert result.template == "IMPULSE"
    assert len(result.points) == 6


def test_advanced_wxy_matrix_allows_double_three_combinations():
    from eliotbot.engine.templates import allowed_child_templates_advanced

    assert "FLAT" in allowed_child_templates_advanced("WXY", 0)
    assert "TRIANGLE" in allowed_child_templates_advanced("WXY", 2)
    assert allowed_child_templates_advanced("IMPULSE", 0) == ("IMPULSE", "DIAGONAL")


def test_wxy_double_three_end_to_end_and_triangle_y():
    from conftest import make_bar

    from eliotbot.engine.hierarchical import structure_variant
    from eliotbot.engine.templates import WXY

    anchors = {
        0: 100,
        7: 120,
        14: 101,
        20: 130,
        27: 115,
        34: 125,
        40: 110,
        44: 130,
        48: 115,
        52: 126,
        56: 117,
        60: 121,
    }
    keys = sorted(anchors)

    def price_at(index):
        if index in anchors:
            return anchors[index]
        for left, right in zip(keys, keys[1:], strict=False):
            if left <= index <= right:
                return anchors[left] + (anchors[right] - anchors[left]) * (index - left) / (
                    right - left
                )
        return anchors[keys[-1]]

    bars = [make_bar(index, price_at(index)) for index in range(61)]

    def point(index, kind, scale=0.7):
        bar = bars[index]
        return PivotPoint(
            pivot_id=f"p{index}",
            index=index,
            extreme_bar_id=bar.bar_id,
            extreme_at=bar.start_at,
            confirmed_at=bar.end_at,
            price=bar.close,
            kind=kind,
            state="CONFIRMED",
            scale=scale,
            threshold_at_extreme=1,
        )

    parent = (
        point(0, "LOW", 1.4),
        point(20, "HIGH", 1.4),
        point(40, "LOW", 1.4),
        point(60, "HIGH", 1.4),
    )
    inner = (
        point(7, "HIGH"),
        point(14, "LOW"),
        point(27, "LOW"),
        point(34, "HIGH"),
        point(44, "HIGH"),
        point(48, "LOW"),
        point(52, "HIGH"),
        point(56, "LOW"),
    )
    structure = build_structure(
        Candidate(WXY, parent, parent[-1], 1, "COMPLETE", (), (1.4,)),
        bars,
        [ZigZagResult(inner, None)],
        as_of=_as_of(),
        coordinate_id="wxy",
        depth=2,
        budget=SearchBudget(200),
        timeframe="1d",
    )
    assert structure.status == "PENDING" and structure.verified_depth == 1
    assert structure_variant(structure, "WXY") == "DOUBLE_THREE"
    assert [link.status for link in structure.nodes[0].child_links] == [
        "VERIFIED",
        "VERIFIED",
        "VERIFIED",
    ]


def test_advanced_wxy_matrix_rejects_triangle_as_wave_w():
    from eliotbot.engine.templates import allowed_child_templates_advanced

    assert "TRIANGLE" not in allowed_child_templates_advanced("WXY", 0)
    assert "WXY" not in allowed_child_templates_advanced("WXY", 2)


def test_full_variant_parent_position_matrix():
    from eliotbot.engine.templates import variant_allowed_in_parent

    assert variant_allowed_in_parent("LEADING_DIAGONAL", "IMPULSE", "1")
    assert variant_allowed_in_parent("LEADING_DIAGONAL", "ZIGZAG_ABC", "A")
    assert not variant_allowed_in_parent("LEADING_DIAGONAL", "IMPULSE", "5")
    assert variant_allowed_in_parent("ENDING_DIAGONAL", "IMPULSE", "5")
    assert not variant_allowed_in_parent("ENDING_DIAGONAL", "IMPULSE", "1")
    assert variant_allowed_in_parent("TRUNCATED_FIFTH", "IMPULSE", "5")
    assert not variant_allowed_in_parent("TRUNCATED_FIFTH", "IMPULSE", "3")
    assert variant_allowed_in_parent("REGULAR_FLAT", "IMPULSE", "2")
    assert variant_allowed_in_parent("REGULAR_FLAT", "WXY", "X")
    assert not variant_allowed_in_parent("REGULAR_FLAT", "IMPULSE", "3")
    assert variant_allowed_in_parent("BARRIER_TRIANGLE", "IMPULSE", "4")
    assert not variant_allowed_in_parent("BARRIER_TRIANGLE", "IMPULSE", "1")
    assert variant_allowed_in_parent("DOUBLE_THREE", "ZIGZAG_ABC", "B")
    assert not variant_allowed_in_parent("DOUBLE_THREE", "IMPULSE", "1")
    assert variant_allowed_in_parent("LEADING_DIAGONAL", None, None)


def test_brief_compares_multiple_automatic_windows():
    from eliotbot.brief import build_brief

    source = _daily_from_weekly_demo()
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=source.as_of),
        source,
        Settings(analysis={"engine_profile": "hierarchical_v1"}),
        include_weekly=False,
        check_windows=True,
    )
    comparison = run.brief.window_comparison
    assert comparison is not None
    assert len(comparison.participants) >= 2
    assert comparison.participants[0].reason == "主窗口"
    assert comparison.total_expansions >= run.result.diagnostics.expansions


def test_second_alternate_window_can_report_a_direction_change():
    from eliotbot.brief import _advanced_windows
    from eliotbot.schemas.brief import BriefAnchor

    data = _daily_from_weekly_demo()
    req = AnalysisRequest(symbol="AAPL", as_of=data.as_of)
    base = AnalysisService().analyze_bundle(req, data)
    scenario = base.scenarios[0].model_copy(update={"next_leg_bias": "UP"})
    origin = scenario.completed_waves[0].pivot
    main, earlier, later = [data.bars[origin.index + offset].session_date for offset in (-2, 2, 4)]
    base = base.model_copy(
        update={
            "scenarios": (scenario,),
            "status": "OK",
            "diagnostics": base.diagnostics.model_copy(update={"anchors": (main, earlier, later)}),
        }
    )
    label = "MACRO_HIGH" if origin.kind == "LOW" else "MACRO_LOW"
    context = SimpleNamespace(
        result=base,
        anchors=(
            BriefAnchor(date=main, origin=label),
            BriefAnchor(date=earlier, origin="FALLBACK"),
            BriefAnchor(date=later, origin=label),
        ),
    )
    visited = []

    def alternate(request, prepared, *, shared_budget):
        assert request.anchor is None
        visited.append(request.start)
        shared_budget.take()
        choice = scenario.model_copy(
            update={"next_leg_bias": "UP" if request.start == later else "DOWN"}
        )
        return SimpleNamespace(
            result=base.model_copy(
                update={
                    "scenarios": (choice,),
                    "diagnostics": base.diagnostics.model_copy(
                        update={"anchors": (request.start,)}
                    ),
                }
            )
        )

    service = SimpleNamespace(analyze_with_context=alternate)
    comparison, _, _ = _advanced_windows(req, data, context, service, SearchBudget(10))
    assert comparison.state == "CHANGED_BIAS"
    assert comparison.alternate_start == earlier
    assert visited == [later, earlier]
    assert len(comparison.participants) == 3 and comparison.total_expansions == 2
