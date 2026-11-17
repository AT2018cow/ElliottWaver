import numpy as np
import pytest
from conftest import make_bar, make_pivots

from eliotbot.engine.indicators import calculate, ema
from eliotbot.engine.matcher import Candidate
from eliotbot.engine.projector import project
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.scorer import weight_scenarios
from eliotbot.engine.templates import IMPULSE
from eliotbot.engine.zigzag import extract
from eliotbot.schemas.market import AnalysisRequest, Bar
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
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, top_k=10)
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
