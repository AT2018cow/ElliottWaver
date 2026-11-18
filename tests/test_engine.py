import numpy as np
import pytest
from conftest import make_bar, make_pivots

from eliotbot.config import SearchConfig, Settings
from eliotbot.engine.degree import wave_degree
from eliotbot.engine.indicators import calculate, ema
from eliotbot.engine.matcher import Candidate, search
from eliotbot.engine.parents import attach_parent_context
from eliotbot.engine.projector import project
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.scorer import score, weight_scenarios
from eliotbot.engine.subwaves import SubwaveCertification, certify_subwaves
from eliotbot.engine.templates import DIAGONAL, FLAT, IMPULSE, WXY, ZIGZAG
from eliotbot.engine.zigzag import ZigZagResult, extract
from eliotbot.schemas.market import AnalysisRequest, Bar
from eliotbot.schemas.wave import PivotPoint
from eliotbot.service import AnalysisService


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
        ([100, 130, 115, 139, 131, 156], "W3_NOT_SHORTEST"),
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


@pytest.mark.parametrize("end, valid", [(154, True), (155, True), (156, False)])
def test_wave5_cap_and_mirror(end, valid):
    bars, points = make_pivots([100, 130, 115, 139, 131, end], developing=True)
    rules = check_pattern("IMPULSE", points, bars, 1, developing=True)
    assert (not any(r.status == "FAIL" for r in rules)) == valid
    mirrored = [
        Bar(
            **{
                **b.model_dump(),
                "open": 300 - b.open,
                "close": 300 - b.close,
                "high": 300 - b.low,
                "low": 300 - b.high,
            }
        )
        for b in bars
    ]
    flipped = tuple(
        p.model_copy(update={"price": 300 - p.price, "kind": "HIGH" if p.kind == "LOW" else "LOW"})
        for p in points
    )
    other = check_pattern("IMPULSE", flipped, mirrored, -1, developing=True)
    assert [(r.rule_id, r.status) for r in rules] == [(r.rule_id, r.status) for r in other]


def test_hidden_wick_cannot_be_skipped_by_merge():
    prices = [100, 110, 120, 115, 110, 130, 150, 140, 132, 145, 152]
    bars = [make_bar(i, value) for i, value in enumerate(prices)]
    _, pivots = make_pivots([100, 120, 110, 150, 132, 152])
    points = tuple(p.model_copy(update={"index": i * 2}) for i, p in enumerate(pivots))
    bars[7] = make_bar(7, 140, low=119)
    rules = check_pattern("IMPULSE", points, bars, 1, developing=False)
    assert next(r for r in rules if r.rule_id == "W4_NO_OVERLAP").status == "FAIL"


def test_projection_respects_fifth_limit():
    bars, pivots = make_pivots([100, 130, 115, 139, 131, 134], developing=True)
    candidate = Candidate(IMPULSE, pivots[:-1], pivots[-1], 1, "W5_DEVELOPING", (), (1.4,))
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    cap = next(c for c in conditions if c.kind == "INVALIDATION")
    assert cap.price == 155 and cap.op == ">" and cap.side == "HIGH"
    assert all(zone.high <= 155 for zone in zones)
    assert any(c.kind == "STAGE_EXIT" and c.price == 131 for c in conditions)


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
    import eliotbot.service as service_module
    from eliotbot.config import Settings

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
    from eliotbot.config import Settings

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
    from eliotbot.config import Settings

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


def directed_pivots(prices, direction):
    bars, points = make_pivots(prices if direction == 1 else [300 - p for p in prices])
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
    bars, points = directed_pivots([100, 130, 115, 141, 128, 149], direction)
    rules = check_pattern("DIAGONAL", points, bars, direction, developing=True)
    assert all(r.status == "PASS" for r in rules)
    candidate = Candidate(
        DIAGONAL, points[:-1], points[-1], direction, "W5_DEVELOPING", rules, (1.4,)
    )
    conditions, zones, _ = project(candidate, bars, 1, "test", bars[-1].end_at, 0.01)
    cap = 154 if direction > 0 else 146
    side, operator = ("HIGH", ">") if direction > 0 else ("LOW", "<")
    assert any(
        c.kind == "INVALIDATION" and c.price == cap and c.side == side and c.op == operator
        for c in conditions
    )
    assert zones
    assert all(zone.high <= cap if direction > 0 else zone.low >= cap for zone in zones)
    breached_bars, breached_points = directed_pivots([100, 130, 115, 141, 128, 155], direction)
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


def test_parent_context_links_child_inside_higher_degree_leg():
    _, parent_pivots = make_pivots([100, 200, 150])
    parent_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 3.8})
        for pivot, index in zip(parent_pivots, (0, 150, 300), strict=True)
    )
    parent = Candidate(ZIGZAG, parent_points, parent_points[-1], 1, "COMPLETE", (), (3.8,))
    _, child_pivots = make_pivots([110, 130, 120, 140])
    child_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(child_pivots, (20, 40, 60, 80), strict=True)
    )
    child = Candidate(ZIGZAG, child_points, child_points[-1], 1, "COMPLETE", (), (1.4,))

    link = attach_parent_context([parent, child], "1d")[child.key]

    assert link.status == "VERIFIED"
    assert link.relation == "CONTAINED"
    assert link.parent_key == parent.key
    assert link.parent_pattern == "ZIGZAG_ABC"
    assert link.parent_degree == "PRIMARY"
    assert link.wave_label == "A"


def test_parent_context_marks_exact_leg_and_developing_leg():
    _, parent_pivots = make_pivots([100, 200, 150])
    parent_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 3.8})
        for pivot, index in zip(parent_pivots, (0, 150, 1000), strict=True)
    )
    parent = Candidate(ZIGZAG, parent_points, parent_points[-1], 1, "COMPLETE", (), (3.8,))
    _, child_pivots = make_pivots([100, 130, 110, 200])
    child_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(child_pivots, (0, 50, 100, 150), strict=True)
    )
    child = Candidate(ZIGZAG, child_points, child_points[-1], 1, "COMPLETE", (), (1.4,))
    exact = attach_parent_context([parent, child], "1d")[child.key]
    assert exact.status == "VERIFIED"
    assert exact.relation == "EXACT_LEG"
    assert exact.wave_label == "A"

    developing_parent = Candidate(
        ZIGZAG,
        parent_points[:2],
        parent_points[2],
        1,
        "B_DEVELOPING",
        (),
        (3.8,),
    )
    _, down_pivots = make_pivots([190, 170, 180, 150])
    down_points = tuple(
        pivot.model_copy(update={"index": index, "scale": 1.4})
        for pivot, index in zip(down_pivots, (180, 220, 260, 300), strict=True)
    )
    down = Candidate(ZIGZAG, down_points, down_points[-1], -1, "COMPLETE", (), (1.4,))
    provisional = attach_parent_context([developing_parent, down], "1d")[down.key]
    assert provisional.status == "PROVISIONAL"
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


def test_wave_degree_thresholds():
    _, pivots = make_pivots([100, 120, 110, 150, 135, 160])
    start = pivots[0].model_copy(update={"index": 0})
    weekly_cycle = pivots[-1].model_copy(update={"index": 400})
    daily_primary = pivots[-1].model_copy(update={"index": 400})
    minor = pivots[-1].model_copy(update={"index": 10})
    assert wave_degree((start, weekly_cycle), "1w") == "CYCLE"
    assert wave_degree((start, daily_primary), "1d") == "PRIMARY"
    assert wave_degree((start, minor), "1d") == "MINOR"


def test_subwave_certification_accepts_complete_five_three_structure():
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
    import eliotbot.service as service_module
    from eliotbot.schemas.wave import SubwaveLegEvidence

    monkeypatch.setattr(
        service_module,
        "certify_subwaves",
        lambda *args: SubwaveCertification(
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
