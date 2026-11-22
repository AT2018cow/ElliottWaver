import numpy as np
import pytest
from conftest import make_bar, make_pivots

from elliottwaver.config import SearchConfig, Settings
from elliottwaver.engine.degree import wave_degree
from elliottwaver.engine.indicators import calculate, ema
from elliottwaver.engine.matcher import Candidate, search
from elliottwaver.engine.parents import attach_parent_context
from elliottwaver.engine.projector import project
from elliottwaver.engine.rules import check_pattern
from elliottwaver.engine.scorer import score, weight_scenarios
from elliottwaver.engine.subwaves import (
    SubwaveCertification,
    certify_subwaves,
    classify_triangle_legs,
)
from elliottwaver.engine.templates import (
    DIAGONAL,
    FLAT,
    IMPULSE,
    TRIANGLE,
    WXY,
    ZIGZAG,
    allowed_child_templates,
)
from elliottwaver.engine.zigzag import ZigZagResult, extract
from elliottwaver.evaluation.events import condition_hit
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.wave import PivotPoint, SubwaveLegEvidence
from elliottwaver.service import AnalysisService


def test_indicator_initialization_is_independently_derived():
    bars = [
        make_bar(0, 10, high=11, low=9),
        make_bar(1, 13, high=14, low=12),
        make_bar(2, 12, high=13, low=11),
        make_bar(3, 15, high=16, low=14),
    ]
    atr = calculate(bars, period=3).atr
    assert np.isnan(atr[:2]).all()
    assert atr[2] == pytest.approx(8 / 3)
    assert atr[3] == pytest.approx(28 / 9)
    np.testing.assert_allclose(ema([1, 2, 3], 3), [1, 1.5, 2.25])
    constant = calculate([make_bar(i, 10) for i in range(30)])
    assert np.all(constant.histogram == 0)
    assert not extract([make_bar(i, 10) for i in range(30)], constant.atr, 1.4).confirmed


def test_confirmed_pivots_are_prefix_invariant(bundle):
    full = extract(bundle.bars, calculate(bundle.bars).atr, 1.4)
    for length in range(20, len(bundle.bars) + 1, 7):
        bars = bundle.bars[:length]
        partial = extract(bars, calculate(bars).atr, 1.4)
        visible = tuple(p for p in full.confirmed if p.confirmed_at <= bars[-1].usable_at)
        assert partial.confirmed == visible
        assert all(p.confirmed_at >= bundle.bars[p.index].end_at for p in partial.confirmed)


def test_outside_bar_does_not_invent_two_pivots():
    prices = [10, 10.3, 12, 14, 13]
    bars = [make_bar(i, price) for i, price in enumerate(prices)]
    bars[3] = make_bar(3, 11, high=16, low=9)
    atr = np.ones(len(bars))
    before = extract(bars[:4], atr[:4], 1)
    after = extract(bars, atr, 1)
    assert not any(p.index == 3 and p.kind == "HIGH" for p in before.confirmed)
    assert any(p.index == 3 and p.confirmed_at == bars[4].end_at for p in after.confirmed)


@pytest.mark.parametrize(
    "prices, failed",
    [
        ([100, 120, 99, 150, 132, 152], "W2_OR_B_ORIGIN"),
        ([100, 120, 110, 150, 120, 152], "W4_NO_OVERLAP"),
        ([100, 140, 132, 151, 144, 174], "W3_NOT_SHORTEST"),
    ],
)
def test_hard_rule_failures(prices, failed):
    bars, points = make_pivots(prices)
    rules = check_pattern("IMPULSE", points, bars, 1, developing=False)
    assert any(r.rule_id == failed and r.status == "FAIL" for r in rules)


def test_developing_third_is_pending_not_shortest_failure():
    bars, points = make_pivots([100, 120, 110, 115], developing=True)
    rules = check_pattern("IMPULSE", points, bars, 1, developing=True)
    assert not any(r.status == "FAIL" for r in rules)
    assert next(r for r in rules if r.rule_id == "W3_NOT_SHORTEST").status == "PENDING"
    assert next(r for r in rules if r.rule_id == "W3_PROGRESS").status == "PENDING"


@pytest.mark.parametrize(
    "prices, direction, expected_pct, expected_boundary, expected",
    [
        ([100, 140, 120, 164, 148, 210], 1, 44 / 120, 40 / 100, "FAIL"),
        ([100, 140, 120, 164, 148, 210], -1, 44 / 180, 40 / 200, "PASS"),
        ([100, 130, 108, 136, 132, 161], 1, 28 / 108, 29 / 132, "PASS"),
        ([100, 130, 108, 136, 132, 161], -1, 28 / 192, 30 / 200, "FAIL"),
    ],
)
def test_completed_wave3_lengths_use_separate_start_prices(
    prices, direction, expected_pct, expected_boundary, expected
):
    bars, points = directed_pivots(prices, direction)
    rules = check_pattern("IMPULSE", points, bars, direction, developing=False)
    shortest = next(rule for rule in rules if rule.rule_id == "W3_NOT_SHORTEST")
    assert shortest.status == expected
    assert shortest.observed == pytest.approx(100 * expected_pct)
    assert shortest.boundary == pytest.approx(100 * expected_boundary)
    matches, _ = search(
        bars, [ZigZagResult(points, points[-1])], [bars[0].session_date], SearchConfig()
    )
    assert any(c.template == IMPULSE and c.stage == "COMPLETE" for c in matches) == (
        expected == "PASS"
    )


def test_hidden_wick_cannot_be_skipped_by_merge():
    prices = [100, 110, 120, 115, 110, 130, 150, 140, 132, 145, 152]
    bars = [make_bar(i, value) for i, value in enumerate(prices)]
    _, pivots = make_pivots([100, 120, 110, 150, 132, 152])
    points = tuple(p.model_copy(update={"index": i * 2}) for i, p in enumerate(pivots))
    bars[7] = make_bar(7, 140, low=119)
    rules = check_pattern("IMPULSE", points, bars, 1, developing=False)
    assert next(r for r in rules if r.rule_id == "W4_NO_OVERLAP").status == "FAIL"


@pytest.mark.parametrize("direction, boundary", [(1, 18209 / 115), (-1, 27209 / 185)])
def test_wave5_percentage_boundary_is_shared_by_rules_projection_and_events(direction, boundary):
    bars, pivots = directed_pivots([100, 130, 115, 139, 131, 134], direction, developing=True)
    candidate = Candidate(IMPULSE, pivots[:-1], pivots[-1], direction, "W5_DEVELOPING", (), (1.4,))
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    cap = next(c for c in conditions if c.kind == "INVALIDATION")
    assert cap.price == boundary
    assert (cap.side, cap.op) == (("HIGH", ">") if direction == 1 else ("LOW", "<"))
    assert zones
    assert all(zone.high <= boundary if direction == 1 else zone.low >= boundary for zone in zones)
    assert any(c.kind == "STAGE_EXIT" and c.price == pivots[4].price for c in conditions)

    for offset, expected in [(-0.01, "PASS"), (0, "PASS"), (0.01, "FAIL")]:
        price = boundary + direction * offset
        bar = make_bar(5, price)
        tip = pivots[-1].model_copy(update={"price": price, "extreme_bar_id": bar.bar_id})
        for developing in (True, False):
            rules = check_pattern(
                "IMPULSE", (*pivots[:-1], tip), [*bars[:-1], bar], direction, developing=developing
            )
            statuses = {rule.rule_id: rule.status for rule in rules}
            assert statuses["W3_NOT_SHORTEST"] == expected
            if developing:
                assert statuses["W5_LENGTH_CAP"] == expected
        assert condition_hit(cap, bar) == (expected == "FAIL")

    price = boundary + direction * 0.01
    close = bars[-1].close
    wick = make_bar(5, close, high=max(price, close), low=min(price, close))
    rules = check_pattern("IMPULSE", pivots, [*bars[:-1], wick], direction, developing=True)
    assert next(rule.status for rule in rules if rule.rule_id == "W5_LENGTH_CAP") == "FAIL"
    assert condition_hit(cap, wick)


@pytest.mark.parametrize(
    "direction, prefix, boundary",
    [(1, [19, 20.15, 20, 21.20, 21], 22.26), (-1, [24, 22.08, 23, 21.62, 22], 20.68)],
)
def test_wave5_cent_quote_touch_is_valid_and_next_tick_breaches(direction, prefix, boundary):
    for offset in (0, 1):
        price = round(boundary + direction * offset * 0.01, 2)
        bars, points = make_pivots([*prefix, price], developing=True)
        if direction < 0:
            points = tuple(
                p.model_copy(update={"kind": "LOW" if p.kind == "HIGH" else "HIGH"}) for p in points
            )
        developing = Candidate(
            IMPULSE, points[:-1], points[-1], direction, "W5_DEVELOPING", (), (1.4,)
        )
        conditions, _, _ = project(developing, bars, 0.1, "test", bars[-1].end_at, 0.01)
        cap = next(c for c in conditions if c.kind == "INVALIDATION")
        assert cap.price == boundary
        assert condition_hit(cap, bars[-1]) is bool(offset)
        for is_developing in (False, True):
            rules = check_pattern("IMPULSE", points, bars, direction, developing=is_developing)
            selected = [r for r in rules if r.rule_id in {"W3_NOT_SHORTEST", "W5_LENGTH_CAP"}]
            assert len(selected) == (2 if is_developing else 1)
            assert all(r.status == ("FAIL" if offset else "PASS") for r in selected)
            if not offset:
                assert all(r.status == "PASS" for r in rules)


@pytest.mark.parametrize(
    "prices, direction",
    [
        ([100, 120, 110, 132, 125, 155], 1),
        ([100, 140, 120, 156, 145, 190], -1),
        ([100, 140, 120, 164, 148, 210], -1),
    ],
)
def test_wave5_has_no_length_cap_when_third_is_at_least_as_long_as_first(prices, direction):
    bars, points = directed_pivots(prices, direction, developing=True)
    rules = check_pattern("IMPULSE", points, bars, direction, developing=True)
    assert all(rule.status == "PASS" for rule in rules)
    assert all(rule.rule_id != "W5_LENGTH_CAP" for rule in rules)
    candidate = Candidate(
        IMPULSE, points[:-1], points[-1], direction, "W5_DEVELOPING", rules, (1.4,)
    )
    conditions, _, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    assert all(condition.kind != "INVALIDATION" for condition in conditions)


def test_complete_projection_includes_deep_retracements():
    bars, pivots = make_pivots([100, 130, 115, 150, 132, 160])
    candidate = Candidate(IMPULSE, pivots, pivots[-1], 1, "COMPLETE", (), (1.4,))
    _, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    assert len(zones) == 5
    assert any(zone.low <= 100 <= zone.high for zone in zones)
    assert any(zone.low <= 112.84 <= zone.high for zone in zones)


def test_complete_projection_deep_retracements_are_direction_symmetric():
    bars, pivots = make_pivots([160, 130, 145, 110, 128, 100])
    candidate = Candidate(IMPULSE, pivots, pivots[-1], -1, "COMPLETE", (), (1.4,))
    _, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    assert len(zones) == 5
    assert any(zone.low <= 160 <= zone.high for zone in zones)
    assert any(zone.low <= 147.16 <= zone.high for zone in zones)


@pytest.mark.parametrize(
    "last, ratio, basis",
    [
        (155, 1.0, "浪1"),
        (187.36, 2.618, "浪1"),
        (199.72, 1.618, "浪3"),
    ],
)
def test_wave5_extension_uses_best_wave1_or_wave3_fit(last, ratio, basis):
    bars, pivots = make_pivots([100, 120, 110, 150, 135, last])
    candidate = Candidate(IMPULSE, pivots, pivots[-1], 1, "COMPLETE", (), (1.4,))
    breakdown = score(candidate, bars, calculate(bars, period=3), volume_comparable=True)
    evidence = next(e for e in breakdown.evidence if e.feature == "extension_5")
    assert evidence.value == pytest.approx(ratio)
    assert evidence.fit == pytest.approx(1.0)
    assert f"相对{basis}" in evidence.explanation


@pytest.mark.parametrize(
    "last, profile, expected_fit",
    [
        (167.36, "ew_score_v1", 0.7),
        (167.36, "ew_score_v2", 1.0),
        (199.72, "ew_score_v1", 0.0),
        (199.72, "ew_score_v2", 1.0),
    ],
)
def test_fifth_wave_profiles_preserve_legacy_and_extended_formulas(last, profile, expected_fit):
    bars, pivots = make_pivots([100, 120, 110, 150, 135, last])
    candidate = Candidate(IMPULSE, pivots, pivots[-1], 1, "COMPLETE", (), (1.4,))
    breakdown = score(
        candidate, bars, calculate(bars, period=3), volume_comparable=True, profile=profile
    )
    evidence = next(e for e in breakdown.evidence if e.feature == "extension_5")
    assert evidence.fit == pytest.approx(expected_fit, abs=1e-12)
    if profile == "ew_score_v1":
        assert "浪3=" not in evidence.explanation
        assert "相对浪1" in evidence.explanation
    else:
        assert "浪3=" in evidence.explanation


def test_unknown_scoring_profile_fails_closed():
    bars, pivots = make_pivots([100, 120, 110, 150, 135, 155])
    candidate = Candidate(IMPULSE, pivots, pivots[-1], 1, "COMPLETE", (), (1.4,))
    with pytest.raises(ValueError, match="Unsupported scoring profile"):
        score(candidate, bars, calculate(bars, period=3), volume_comparable=True, profile="typo")


def test_analysis_service_passes_selected_scoring_profile(bundle, monkeypatch):
    import elliottwaver.service as service_module
    from elliottwaver.config import Settings

    observed = []

    def recording_score(*args, profile, **kwargs):
        observed.append(profile)
        return score(*args, profile=profile, **kwargs)

    monkeypatch.setattr(service_module, "score", recording_score)
    result = AnalysisService(Settings(scoring={"profile": "ew_score_v1"})).analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    assert observed and set(observed) == {"ew_score_v1"}
    assert result.engine_version.endswith("/ew_score_v1")


def test_excluded_structure_is_reported(bundle):
    from elliottwaver.config import Settings

    result = AnalysisService(
        Settings(scoring={"min_quality": 99, "min_coverage": 0})
    ).analyze_bundle(AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle)
    assert not result.scenarios
    assert any(w.startswith("EXCLUDED_STRUCTURE:") for w in result.warnings)


def test_missing_feature_does_not_inflate_fit(bundle):
    service = AnalysisService()
    result = service.analyze_bundle(AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle)
    assert result.scenarios
    for scenario in result.scenarios:
        assert scenario.score.quality_score == pytest.approx(
            scenario.score.fit_score * scenario.score.coverage
        )
        assert scenario.calibrated_event_probability is None
        assert scenario.score.coverage < 1
        assert not any(r.status == "FAIL" for r in scenario.rules)


def test_deterministic_results_and_topk_mass(bundle):
    service = AnalysisService()
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, top_k=20)
    first = service.analyze_bundle(request, bundle)
    second = service.analyze_bundle(
        request.model_copy(update={"mode": "FROZEN_DATA_REPLAY"}), bundle
    )
    assert first.run_id != second.run_id
    assert first.result_digest == second.result_digest
    short, omitted = weight_scenarios(first.scenarios, 12, 1)
    assert short[0].relative_weight == pytest.approx(first.scenarios[0].relative_weight)
    assert omitted == pytest.approx(1 - short[0].relative_weight)
    assert sum(s.relative_weight for s in first.scenarios) + first.omitted_weight == pytest.approx(
        1
    )


def test_search_budget_is_global_and_visible(bundle):
    from elliottwaver.config import Settings

    settings = Settings(search={"max_expansions": 1})
    result = AnalysisService(settings).analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    assert result.status == "SEARCH_LIMITED"
    assert result.diagnostics.search_truncated
    assert result.diagnostics.expansions == 1


@pytest.mark.parametrize(
    "prices, pattern",
    [
        ([100, 120, 101, 119], "FLAT"),
        ([100, 120, 105, 115, 108, 112], "TRIANGLE"),
        ([100, 130, 115, 141, 128, 149], "DIAGONAL"),
        ([100, 120, 105, 130], "WXY"),
    ],
)
def test_new_template_hard_rules(prices, pattern):
    bars, points = make_pivots(prices)
    rules = check_pattern(pattern, points, bars, 1, developing=False)
    assert not any(rule.status == "FAIL" for rule in rules), rules


@pytest.mark.parametrize(
    "prices, pattern",
    [
        ([100, 120, 101, 119], "FLAT"),
        ([100, 120, 105, 115, 108, 112], "TRIANGLE"),
        ([100, 130, 115, 141, 128, 149], "DIAGONAL"),
        ([100, 120, 105, 130], "WXY"),
    ],
)
def test_new_templates_are_enumerated(prices, pattern):
    bars, points = make_pivots(prices)
    zigzag = ZigZagResult(points, points[-1])
    candidates, _ = search(
        bars, [zigzag], [bars[0].session_date], SearchConfig(max_bridge_intervals=5)
    )
    assert any(
        candidate.template.name == pattern and candidate.stage == "COMPLETE"
        for candidate in candidates
    )


def directed_pivots(prices, direction, *, developing=False):
    bars, points = make_pivots(
        prices if direction == 1 else [300 - p for p in prices], developing=developing
    )
    if direction < 0:
        points = tuple(
            p.model_copy(update={"kind": "LOW" if p.kind == "HIGH" else "HIGH"}) for p in points
        )
    return bars, points


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize(
    "prices, rule_id",
    [
        ([100, 130, 125, 145, 128, 160], "W3_NOT_SHORTEST"),
        ([100, 130, 115, 141, 115, 149], "W4_NO_FULL_RETRACE"),
        ([100, 130, 112, 147, 128, 158], "DIAGONAL_SHAPE"),
        ([100, 130, 115, 141, 128, 140], "W5_PROGRESS"),
    ],
)
def test_diagonal_rejects_invalid_lengths_and_geometry(prices, rule_id, direction):
    bars, points = directed_pivots(prices, direction)
    rules = check_pattern("DIAGONAL", points, bars, direction, developing=False)
    assert next(r.status for r in rules if r.rule_id == rule_id) == "FAIL"


@pytest.mark.parametrize("direction", [1, -1])
def test_diagonal_fifth_projection_respects_shortest_third_boundary(direction):
    bars, points = directed_pivots([100, 130, 115, 141, 128, 146], direction, developing=True)
    rules = check_pattern("DIAGONAL", points, bars, direction, developing=True)
    assert all(r.status == "PASS" for r in rules)
    candidate = Candidate(
        DIAGONAL, points[:-1], points[-1], direction, "W5_DEVELOPING", rules, (1.4,)
    )
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    cap = 18048 / 115 if direction > 0 else 27348 / 185
    side, operator = ("HIGH", ">") if direction > 0 else ("LOW", "<")
    assert any(
        c.kind == "INVALIDATION" and c.price == cap and c.side == side and c.op == operator
        for c in conditions
    )
    assert zones
    assert all(zone.high <= cap if direction > 0 else zone.low >= cap for zone in zones)
    breached_bars, breached_points = directed_pivots(
        [100, 130, 115, 141, 128, 157], direction, developing=True
    )
    breached = check_pattern("DIAGONAL", breached_points, breached_bars, direction, developing=True)
    assert next(r.status for r in breached if r.rule_id == "W5_LENGTH_CAP") == "FAIL"


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize(
    "prices, indices, developing, expected",
    [
        ([100, 120, 115, 134, 130, 148], [0, 10, 11, 12, 112, 113], False, "FAIL"),
        ([100, 120, 105, 111, 109, 110], list(range(6)), False, "FAIL"),
        ([100, 120, 109, 125, 105, 131], list(range(6)), False, "PASS"),
        ([100, 120, 105, 115, 108], list(range(5)), True, "PENDING"),
        ([100, 120, 109, 125, 105, 124], list(range(6)), True, "PENDING"),
        ([100, 120, 109, 125, 105, 124], list(range(6)), False, "FAIL"),
    ],
)
def test_triangle_tracks_rails_and_developing_leg(prices, indices, developing, expected, direction):
    _, points = directed_pivots(prices, direction)
    points = tuple(p.model_copy(update={"index": i}) for p, i in zip(points, indices, strict=True))
    by_index = {p.index: p.price for p in points}
    close = points[0].price
    bars = []
    for i in range(indices[-1] + 1):
        close = by_index.get(i, close)
        bars.append(make_bar(i, close))
    rules = check_pattern("TRIANGLE", points, bars, direction, developing=developing)
    assert next(r.status for r in rules if r.rule_id == "TRIANGLE_SHAPE") == expected
    if expected != "FAIL":
        assert not any(r.status == "FAIL" for r in rules)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("end, valid", [(98, False), (100, False), (102, True), (105, True)])
def test_running_triangle_requires_net_retracement_at_confirmed_e(end, valid, direction):
    prices = [100, 110, 90, 107, 94, end]
    for developing in (True, False):
        bars, points = directed_pivots(prices, direction, developing=developing)
        rules = check_pattern("TRIANGLE", points, bars, direction, developing=developing)
        statuses = {rule.rule_id: rule.status for rule in rules}
        assert statuses["TRIANGLE_SHAPE"] == "PASS"
        expected = "PASS" if valid else "PENDING" if developing else "FAIL"
        assert statuses["TRIANGLE_NET_RETRACEMENT"] == expected
        confirmed = points[:-1] if developing else points
        matches, _ = search(
            bars, [ZigZagResult(confirmed, points[-1])], [bars[0].session_date], SearchConfig()
        )
        stage = "E_DEVELOPING" if developing else "COMPLETE"
        assert any(c.template == TRIANGLE and c.stage == stage for c in matches) == (
            valid or developing
        )


@pytest.mark.parametrize("direction", [1, -1])
def test_flat_b_can_cross_a_origin_and_keeps_its_own_conditions(direction):
    bars, points = directed_pivots([100, 120, 99], direction)
    flat_rules = check_pattern("FLAT", points, bars, direction, developing=True)
    assert next(r.status for r in flat_rules if r.rule_id == "B_DEEP") == "PASS"
    zigzag_rules = check_pattern("ZIGZAG_ABC", points, bars, direction, developing=True)
    assert next(r.status for r in zigzag_rules if r.rule_id == "W2_OR_B_ORIGIN") == "FAIL"
    candidate = Candidate(
        FLAT, points[:-1], points[-1], direction, "B_DEVELOPING", flat_rules, (1.4,)
    )
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    assert all(c.kind != "INVALIDATION" for c in conditions)
    assert any(c.kind == "STAGE_EXIT" and c.price == points[1].price for c in conditions)
    assert zones


def test_wxy_y_projection_uses_its_own_endpoints_and_confirmation():
    bars, points = make_pivots([100, 120, 105, 117], developing=True)
    rules = check_pattern("WXY", points, bars, 1, developing=True)
    x_retracement = next(r for r in rules if r.rule_id == "X_RETRACE")
    assert (x_retracement.observed, x_retracement.boundary, x_retracement.status) == (
        15,
        10,
        "PASS",
    )
    assert next(r.status for r in rules if r.rule_id == "Y_BEYOND_W") == "PENDING"
    candidate = Candidate(WXY, points[:-1], points[-1], 1, "Y_DEVELOPING", rules, (1.4,))
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    assert [(c.kind, c.side, c.op, c.price) for c in conditions] == [
        ("INVALIDATION", "LOW", "<", 105),
        ("CONFIRMATION", "HIGH", ">", 120),
    ]
    assert all("双锯齿" in c.reason for c in conditions)
    assert zones and all(zone.low > 120 for zone in zones)


def test_subwave_depth_two_ignores_pivots_outside_the_parent_legs():
    _, pivots = make_pivots([100, 120, 110, 130])
    bars = tuple(make_bar(index, 100) for index in range(41))

    def at(indices):
        return tuple(
            pivots[i % len(pivots)].model_copy(update={"index": index})
            for i, index in enumerate(indices)
        )

    points = at([10, 20, 25, 30])
    parent = Candidate(ZIGZAG, points, points[-1], 1, "COMPLETE", (), (3.8,))
    mid = ZigZagResult(at([0, 11, 16, 22, 40]), None)
    coarse = ZigZagResult(points, None)
    baseline = certify_subwaves(
        parent,
        [ZigZagResult(at([12, 14]), None), mid, coarse],
        [1.4, 2.4, 3.8],
        2,
        bars,
    )
    with_outside = certify_subwaves(
        parent,
        [ZigZagResult(at([2, 4, 12, 14, 32, 34]), None), mid, coarse],
        [1.4, 2.4, 3.8],
        2,
        bars,
    )
    assert baseline.fit is None
    assert with_outside.fit == baseline.fit


def test_parent_context_reports_containment_as_provisional():
    _, parent_pivots = make_pivots([100, 200, 150, 210])
    parent_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 3.8})
        for pivot, index in zip(parent_pivots, (0, 150, 240, 300), strict=True)
    )
    parent = Candidate(ZIGZAG, parent_points, parent_points[-1], 1, "COMPLETE", (), (3.8,))
    _, child_pivots = make_pivots([110, 130, 120, 140])
    child_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(child_pivots, (20, 40, 60, 80), strict=True)
    )
    child = Candidate(ZIGZAG, child_points, child_points[-1], 1, "COMPLETE", (), (1.4,))

    link = attach_parent_context([parent, child], "1d")[child.key]

    assert link.status == "PROVISIONAL"
    assert link.relation == "CONTAINED"
    assert link.parent_key == parent.key
    assert link.parent_pattern == "ZIGZAG_ABC"
    assert link.parent_degree == "PRIMARY"
    assert link.wave_label == "A"


def test_parent_context_marks_exact_leg_and_developing_leg():
    _, parent_pivots = make_pivots([100, 200, 150, 210])
    parent_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 3.8})
        for pivot, index in zip(parent_pivots, (0, 150, 300, 1000), strict=True)
    )
    parent = Candidate(ZIGZAG, parent_points, parent_points[-1], 1, "COMPLETE", (), (3.8,))
    _, child_pivots = make_pivots([100, 130, 115, 160, 145, 200])
    child_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(child_pivots, (0, 25, 50, 75, 100, 150), strict=True)
    )
    child = Candidate(IMPULSE, child_points, child_points[-1], 1, "COMPLETE", (), (1.4,))
    exact = attach_parent_context([parent, child], "1d")[child.key]
    assert exact.status == "VERIFIED"
    assert exact.relation == "EXACT_LEG"
    assert exact.wave_label == "A"

    wrong_end = child_points[-1].model_copy(update={"price": 199})
    mismatched = Candidate(
        IMPULSE, (*child_points[:-1], wrong_end), wrong_end, 1, "COMPLETE", (), (1.4,)
    )
    assert attach_parent_context([parent, mismatched], "1d")[mismatched.key].status == "UNKNOWN"

    _, corrective_pivots = make_pivots([100, 130, 115, 200])
    corrective_points = tuple(
        pivot.model_copy(update={"index": index})
        for pivot, index in zip(corrective_pivots, (0, 50, 100, 150), strict=True)
    )
    corrective = Candidate(
        ZIGZAG, corrective_points, corrective_points[-1], 1, "COMPLETE", (), (1.4,)
    )
    assert attach_parent_context([parent, corrective], "1d")[corrective.key].status == "UNKNOWN"

    developing_parent = Candidate(
        ZIGZAG,
        parent_points[:2],
        parent_points[2].model_copy(update={"state": "DEVELOPING", "confirmed_at": None}),
        1,
        "B_DEVELOPING",
        (),
        (3.8,),
    )
    _, down_pivots = make_pivots([200, 170, 180, 150])
    down_points = tuple(
        pivot.model_copy(update={"index": index, "kind": "HIGH" if pivot.kind == "LOW" else "LOW"})
        for pivot, index in zip(down_pivots, (150, 220, 260, 300), strict=True)
    )
    down = Candidate(ZIGZAG, down_points, down_points[-1], -1, "COMPLETE", (), (1.4,))
    provisional = attach_parent_context([developing_parent, down], "1d")[down.key]
    assert provisional.status == "PROVISIONAL"
    assert provisional.relation == "EXACT_LEG"
    assert provisional.wave_label == "B"


def test_parent_context_is_unknown_when_child_crosses_a_parent_pivot():
    _, parent_pivots = make_pivots([100, 200, 150])
    parent_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 3.8})
        for pivot, index in zip(parent_pivots, (0, 150, 300), strict=True)
    )
    parent = Candidate(ZIGZAG, parent_points, parent_points[-1], 1, "COMPLETE", (), (3.8,))
    _, child_pivots = make_pivots([110, 130, 120, 140])
    child_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(child_pivots, (100, 200, 250, 300), strict=True)
    )
    child = Candidate(ZIGZAG, child_points, child_points[-1], 1, "COMPLETE", (), (1.4,))
    assert attach_parent_context([parent, child], "1d")[child.key].status == "UNKNOWN"


def test_triangle_child_permissions_allow_only_provisional_nested_e():
    for leg_index in range(4):
        allowed = allowed_child_templates("TRIANGLE", leg_index)
        assert "FLAT" in allowed
        assert "TRIANGLE" not in allowed
    assert "FLAT" in allowed_child_templates("TRIANGLE", 4)
    assert "TRIANGLE" in allowed_child_templates("TRIANGLE", 4)
    assert allowed_child_templates("TRIANGLE", 5) == ()
    assert allowed_child_templates("ZIGZAG_ABC", 3) == ()


def test_triangle_e_leg_accepts_flat_and_marks_nested_triangle_provisional():
    parent_spec = [(0, 100), (100, 150), (200, 120), (300, 155), (400, 110), (500, 160)]
    triangle_spec = [(400, 110), (420, 145), (440, 120), (460, 150), (480, 118), (500, 160)]
    flat_spec = [(400, 110), (430, 145), (470, 111), (500, 160)]

    def build(child_spec, child_template):
        path = sorted(dict(parent_spec + child_spec).items())
        prices = [0.0] * (path[-1][0] + 1)
        for (left, start), (right, end) in zip(path, path[1:], strict=False):
            for index in range(left, right + 1):
                prices[index] = start + (end - start) * (index - left) / (right - left)
        bars = tuple(make_bar(index, price) for index, price in enumerate(prices))

        def pivots(spec, scale):
            return tuple(
                PivotPoint(
                    pivot_id=f"p{index}",
                    index=index,
                    extreme_bar_id=bars[index].bar_id,
                    extreme_at=bars[index].start_at,
                    confirmed_at=bars[index].end_at,
                    price=price,
                    kind="LOW" if position % 2 == 0 else "HIGH",
                    state="CONFIRMED",
                    scale=scale,
                    threshold_at_extreme=1,
                )
                for position, (index, price) in enumerate(spec)
            )

        def candidate(template, points):
            rules = check_pattern(template.name, points, bars, 1, developing=False)
            assert all(rule.status == "PASS" for rule in rules), rules
            return Candidate(template, points, points[-1], 1, "COMPLETE", rules, (points[0].scale,))

        return candidate(TRIANGLE, pivots(parent_spec, 3.8)), candidate(
            child_template, pivots(child_spec, 2.4)
        )

    parent, triangle_child = build(triangle_spec, TRIANGLE)
    triangle_link = attach_parent_context([parent, triangle_child], "1d")[triangle_child.key]
    assert triangle_link.status == "PROVISIONAL"
    assert triangle_link.relation == "EXACT_LEG"
    assert triangle_link.parent_pattern == "TRIANGLE"
    assert triangle_link.wave_label == "E"

    parent, flat_child = build(flat_spec, FLAT)
    flat_link = attach_parent_context([parent, flat_child], "1d")[flat_child.key]
    assert flat_link.status == "VERIFIED"
    assert flat_link.relation == "EXACT_LEG"
    assert flat_link.parent_pattern == "TRIANGLE"
    assert flat_link.wave_label == "E"


def test_triangle_group_policy_counts_wxy_as_complex():
    def evidence(patterns):
        return tuple(
            SubwaveLegEvidence(
                leg=f"l{index}",
                status="VERIFIED",
                pattern=pattern,
                point_count=4,
                scale=2.4,
                depth=1,
                reason="test",
            )
            for index, pattern in enumerate(patterns)
        )

    assert classify_triangle_legs(evidence(["ZIGZAG_ABC"] * 5))["classic"]
    assert classify_triangle_legs(evidence(["ZIGZAG_ABC"] * 4 + ["FLAT"]))["classic"]
    assert not classify_triangle_legs(evidence(["ZIGZAG_ABC"] * 3 + ["FLAT"] * 2))["classic"]
    assert not classify_triangle_legs(evidence(["FLAT"] * 5))["classic"]

    wxy_two = classify_triangle_legs(evidence(["ZIGZAG_ABC"] * 3 + ["WXY"] * 2))
    assert wxy_two["zigzag_legs"] == 5
    assert wxy_two["complex_legs"] == 2
    assert not wxy_two["classic"]

    wxy_one = classify_triangle_legs(evidence(["ZIGZAG_ABC"] * 4 + ["WXY"]))
    assert wxy_one["zigzag_legs"] == 5
    assert wxy_one["complex_legs"] == 1
    assert wxy_one["classic"]


def _triangle_child_fixture(mode: str, direction: int = 1):
    outer = ((0, 100), (100, 180), (220, 110), (320, 166), (430, 125), (520, 154))
    path = list(outer)
    finer_only = set()
    for leg, ((left, start), (right, end)) in enumerate(zip(outer, outer[1:], strict=False)):
        if mode == "alternates" and leg < 2:
            width = end - start
            indices = [left + (right - left) * offset // 5 for offset in range(1, 5)]
            path.extend(
                zip(
                    indices,
                    (start + 0.55 * width, start + 0.02 * width, end, start + 0.04 * width),
                    strict=True,
                )
            )
            finer_only.update(indices[:2])
        else:
            a, b = (
                (end, start)
                if mode == "five_flats"
                else (start + 0.7 * (end - start), start + 0.35 * (end - start))
            )
            step = (right - left) // 3
            path.extend(((left + step, a), (left + 2 * step, b)))
    path.sort()
    if direction < 0:
        path = [(index, 300 - price) for index, price in path]
    prices = [0.0] * (path[-1][0] + 1)
    for (left, start), (right, end) in zip(path, path[1:], strict=False):
        for index in range(left, right + 1):
            prices[index] = start + (end - start) * (index - left) / (right - left)
    bars = tuple(make_bar(index, price) for index, price in enumerate(prices))
    finer = tuple(
        PivotPoint(
            pivot_id=f"p{index}",
            index=index,
            extreme_bar_id=bars[index].bar_id,
            extreme_at=bars[index].start_at,
            confirmed_at=bars[index].end_at,
            price=price,
            kind="LOW" if (position % 2 == 0) == (direction > 0) else "HIGH",
            state="CONFIRMED",
            scale=1.4,
            threshold_at_extreme=1,
        )
        for position, (index, price) in enumerate(path)
    )
    coarser = tuple(p.model_copy(update={"scale": 2.4}) for p in finer if p.index not in finer_only)
    by_index = {point.index: point for point in finer}
    parent_points = tuple(by_index[index].model_copy(update={"scale": 3.8}) for index, _ in outer)
    rules = check_pattern("TRIANGLE", parent_points, bars, direction, developing=False)
    assert all(rule.status == "PASS" for rule in rules), rules
    parent = Candidate(
        TRIANGLE, parent_points, parent_points[-1], direction, "COMPLETE", rules, (3.8,)
    )
    return bars, parent, coarser, finer


def test_triangle_group_policy_rejects_five_flats_and_accepts_five_zigzags():
    bars, parent, coarser, _ = _triangle_child_fixture("five_flats")
    flats = certify_subwaves(parent, [ZigZagResult(coarser, None)], [2.4], 1, bars)
    assert flats.status == "UNVERIFIED"
    assert flats.fit == pytest.approx(0.0)
    assert [item.pattern for item in flats.evidence] == ["FLAT"] * 5

    bars, parent, coarser, _ = _triangle_child_fixture("five_zigzags")
    zigzags = certify_subwaves(parent, [ZigZagResult(coarser, None)], [2.4], 1, bars)
    assert zigzags.status == "VERIFIED"
    assert zigzags.fit == pytest.approx(1.0)
    assert [item.pattern for item in zigzags.evidence] == ["ZIGZAG_ABC"] * 5


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("depth", [1, 2])
def test_classic_triangle_finds_zigzag_alternatives_at_finer_scale(direction, depth):
    bars, parent, coarser, finer = _triangle_child_fixture("alternates", direction)
    zigzags = [ZigZagResult(finer, None), ZigZagResult(coarser, None)]
    loose = certify_subwaves(parent, zigzags, [1.4, 2.4], depth, bars, classic_triangles=False)
    assert [item.pattern for item in loose.evidence][:2] == ["FLAT", "FLAT"]
    classic = certify_subwaves(parent, zigzags, [1.4, 2.4], depth, bars)
    assert classic.status == "VERIFIED"
    assert classic.fit == pytest.approx(1.0)
    assert sum(item.pattern == "ZIGZAG_ABC" for item in classic.evidence) >= 4
    assert any(item.scale == 1.4 for item in classic.evidence[:2])


def test_wave_degree_thresholds():
    _, pivots = make_pivots([100, 120, 110, 150, 135, 160])
    start = pivots[0].model_copy(update={"index": 0})
    weekly_cycle = pivots[-1].model_copy(update={"index": 400})
    daily_primary = pivots[-1].model_copy(update={"index": 400})
    minor = pivots[-1].model_copy(update={"index": 10})
    assert wave_degree((start, weekly_cycle), "1w") == "CYCLE"
    assert wave_degree((start, daily_primary), "1d") == "PRIMARY"
    assert wave_degree((start, minor), "1d") == "MINOR"


@pytest.mark.parametrize("third_is_diagonal", [False, True])
def test_subwave_certification_requires_impulse_in_third_wave(third_is_diagonal):
    parent_prices = [100, 120, 110, 150, 135, 160]
    parent_indices = [0, 10, 20, 30, 40, 50]
    inner = {
        2: (105, "HIGH"),
        4: (102, "LOW"),
        6: (112, "HIGH"),
        8: (106, "LOW"),
        12: (116, "LOW"),
        16: (119, "HIGH"),
        22: (125, "HIGH"),
        24: (118, "LOW"),
        26: (140, "HIGH"),
        28: (132, "LOW"),
        32: (144, "LOW"),
        36: (148, "HIGH"),
        42: (145, "HIGH"),
        44: (140, "LOW"),
        46: (155, "HIGH"),
        48: (148, "LOW"),
    }
    if third_is_diagonal:
        # Contracting diagonal scaled from [100,130,115,141,128,149] onto [110,150].
        for index, offset in zip((22, 24, 26, 28), (30, 15, 41, 28), strict=True):
            inner[index] = (110 + offset * 40 / 49, inner[index][1])
    path = sorted(
        [
            *zip(parent_indices, parent_prices, strict=True),
            *((index, price) for index, (price, _) in inner.items()),
        ]
    )
    prices = [0.0] * (parent_indices[-1] + 1)
    for (left, start), (right, end) in zip(path, path[1:], strict=False):
        for index in range(left, right + 1):
            fraction = (index - left) / (right - left)
            prices[index] = start + fraction * (end - start)
    bars = tuple(make_bar(index, price) for index, price in enumerate(prices))
    points = tuple(
        PivotPoint(
            pivot_id=f"p{index}",
            index=index,
            extreme_bar_id=bars[index].bar_id,
            extreme_at=bars[index].start_at,
            confirmed_at=bars[index].end_at,
            price=price,
            kind="LOW" if index % 20 == 0 else "HIGH",
            state="CONFIRMED",
            scale=3.8,
            threshold_at_extreme=1,
        )
        for index, price in zip(parent_indices, parent_prices, strict=True)
    )
    finer = tuple(
        PivotPoint(
            pivot_id=f"f{index}",
            index=index,
            extreme_bar_id=bars[index].bar_id,
            extreme_at=bars[index].start_at,
            confirmed_at=bars[index].end_at,
            price=price,
            kind=kind,
            state="CONFIRMED",
            scale=2.4,
            threshold_at_extreme=1,
        )
        for index, (price, kind) in sorted(inner.items())
    )
    candidate = Candidate(IMPULSE, points, points[-1], 1, "COMPLETE", (), (3.8,))
    result = certify_subwaves(candidate, [ZigZagResult(finer, finer[-1])], [2.4], 1, bars)
    third_template = DIAGONAL if third_is_diagonal else IMPULSE
    third = (points[2], *(p for p in finer if 20 < p.index < 30), points[3])
    rules = check_pattern(third_template.name, third, bars, 1, developing=False)
    assert all(rule.status == "PASS" for rule in rules)
    child = Candidate(third_template, third, third[-1], 1, "COMPLETE", rules, (2.4,))
    link = attach_parent_context([candidate, child], "1w")[child.key]
    if third_is_diagonal:
        assert link.status == "UNKNOWN"
        assert result.status == "UNVERIFIED"
        assert result.fit == pytest.approx(0.8)
        assert result.evidence[2].leg == "2->3"
        assert result.evidence[2].status == "UNVERIFIED"
        assert result.evidence[2].pattern is None
        return
    assert link.status == "VERIFIED"
    assert link.wave_label == "3"
    assert result.status == "VERIFIED"
    assert result.fit == pytest.approx(1.0)
    assert [item.status for item in result.evidence] == ["VERIFIED"] * 5
    assert [item.pattern for item in result.evidence] == [
        "IMPULSE",
        "ZIGZAG_ABC",
        "IMPULSE",
        "ZIGZAG_ABC",
        "IMPULSE",
    ]


def test_subwave_evidence_is_available_when_enabled(bundle, monkeypatch):
    import elliottwaver.service as service_module
    from elliottwaver.schemas.wave import SubwaveLegEvidence

    monkeypatch.setattr(
        service_module,
        "certify_subwaves",
        lambda *args, **kwargs: SubwaveCertification(
            "VERIFIED",
            1.0,
            (
                SubwaveLegEvidence(
                    leg="0->1",
                    status="VERIFIED",
                    pattern="IMPULSE",
                    point_count=6,
                    scale=2.4,
                    depth=1,
                    reason="测试子结构",
                ),
            ),
            1,
        ),
    )
    result = AnalysisService(Settings(search={"max_subwave_depth": 1})).analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    evidence = [
        item
        for scenario in result.scenarios
        for item in scenario.score.evidence
        if item.feature == "subwave_structure"
    ]
    assert any(item.status == "AVAILABLE" for item in evidence)
    assert any(scenario.subwave_status == "VERIFIED" for scenario in result.scenarios)
    assert any(scenario.subwave_evidence for scenario in result.scenarios)
