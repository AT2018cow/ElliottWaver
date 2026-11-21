"""Exercise variant certification with actual OHLC, including the old false positives."""

from itertools import pairwise

import pytest

from eliotbot.brief import build_brief
from eliotbot.config import Settings
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService
from eliotbot.visualization.brief_formatter import brief_html, brief_markdown
from scripts.make_variant_fixtures import PATHS, PER, _bundle


def _settings(profile="hierarchical_v1", depth=1) -> Settings:
    return Settings(
        analysis={
            "engine_profile": profile,
            "scales": (0.5, 1.0, 1.4, 2.4, 3.8, 8.0, 16.0),
            "top_k": 20,
            "min_anchor_bars": 25,
            "max_analysis_bars": 1600,
        },
        search={
            "max_subwave_depth": depth,
            "max_expansions": 100000,
            "beam_width": 1024,
            "max_bridge_intervals": 21,
        },
        scoring={"min_quality": 0, "min_coverage": 0.1, "unclear_below": 0},
    )


def _segmented(endpoints, counts):
    prices = [endpoints[0]]
    for (start, end), count in zip(pairwise(endpoints), counts, strict=True):
        fractions = (0.34, 0.18, 0.71, 0.56, 1) if count == 5 else (0.62, 0.31, 1)
        prices.extend(start + (end - start) * f for f in fractions)
    return prices


@pytest.mark.parametrize(
    ("prices", "density", "subtype", "wave_count"),
    [
        (
            [65, 175, *_segmented([100, 120, 105, 116, 105, 111], (3,) * 5), 94],
            12,
            "BARRIER_TRIANGLE",
            6,
        ),
        ([65, 175, *_segmented([100, 140, 101, 141], (3, 3, 5)), 120], 12, "REGULAR_FLAT", 4),
        ([65, 175, *_segmented([100, 140, 92, 145], (3, 3, 5)), 123], 8, "EXPANDED_FLAT", 4),
        ([65, 175, *_segmented([100, 140, 92, 127], (3, 3, 5)), 104], 12, "RUNNING_FLAT", 4),
        (
            [65, 175, 100, 125, 113, 152, 135, 140, 137.75, 146, 141, 149, 130],
            12,
            "TRUNCATED_FIFTH",
            6,
        ),
        ([60, 205, 100, 170, 106, 174, 150, 157, 146, 165, 153, 172, 118], 8, "DOUBLE_THREE", 4),
    ],
)
def test_certifiable_variant_matches_analyze_and_brief(prices, density, subtype, wave_count):
    bundle = _bundle("900099.SZ", prices, per=density)
    request = AnalysisRequest(symbol="900099.SZ", as_of=bundle.as_of)
    settings = _settings()
    result = AnalysisService(settings).analyze_bundle(request, bundle)
    scenario = next(s for s in result.scenarios if s.variant == subtype and s.stage == "COMPLETE")
    assert len(scenario.completed_waves) == wave_count
    assert all(w.pivot.state == "CONFIRMED" for w in scenario.completed_waves)
    assert scenario.structure is not None
    assert all(node.requested_depth == 0 for node in scenario.structure.nodes[1:])
    if subtype == "TRUNCATED_FIFTH":
        last = scenario.structure.nodes[0].child_links[-1]
        proof = next(
            node for node in scenario.structure.nodes if node.node_id == last.child_node_id
        )
        assert last.status == "VERIFIED" and len(proof.points) == 6
    else:
        assert scenario.structure.status == "VERIFIED"
    run = build_brief(request, bundle, settings, include_weekly=False, check_windows=False)
    assert run.result.result_digest == result.result_digest
    if subtype == "DOUBLE_THREE":
        p = [w.pivot.price for w in scenario.completed_waves]
        assert p[1] - p[2] < (p[1] - p[0]) / 2 and p[3] < p[1]
        assert subtype in brief_markdown(run.brief, run.result)
        assert subtype in brief_html(run.brief, run.result)


@pytest.mark.parametrize(
    ("name", "variant"),
    [
        ("barrier_triangle", "BARRIER_TRIANGLE"),
        ("truncated_fifth", "TRUNCATED_FIFTH"),
        ("flat_running", "RUNNING_FLAT"),
    ],
)
def test_previous_unproven_fixtures_are_no_longer_certified(name, variant):
    bundle = _bundle("900099.SZ", PATHS[name], per=PER.get(name, 16))
    request = AnalysisRequest(symbol="900099.SZ", as_of=bundle.as_of)
    for profile in ("hierarchical_v1", "classic_v4"):
        result = AnalysisService(_settings(profile)).analyze_bundle(request, bundle)
        assert all(s.variant != variant for s in result.scenarios)


def test_depth_zero_disables_variant_certification_and_keeps_classic_default_isolated():
    prices = [65, 175, 100, 125, 113, 152, 135, 140, 137.75, 146, 141, 149, 130]
    bundle = _bundle("900099.SZ", prices, per=12)
    request = AnalysisRequest(symbol="900099.SZ", as_of=bundle.as_of)
    for profile in ("classic_v4", "hierarchical_v1"):
        run = AnalysisService(_settings(profile, depth=0)).analyze_bundle(request, bundle)
        assert all(
            s.variant != "TRUNCATED_FIFTH" and s.subwave_status == "UNAVAILABLE"
            for s in run.scenarios
        )
        assert all(s.structure is None or s.structure.verified_depth == 0 for s in run.scenarios)
        if profile == "classic_v4":
            assert all(s.structure is None and s.variant is None for s in run.scenarios)


def test_leading_diagonal_requires_real_parent_position_and_uniform_subdivision():
    prices = [20, 140, 35, 120, *PATHS["leading_diagonal"]]
    bundle = _bundle("900099.SZ", prices, per=12)
    request = AnalysisRequest(
        symbol="900099.SZ",
        as_of=bundle.as_of,
        anchor=bundle.bars[24].session_date,
    )
    result = AnalysisService(_settings()).analyze_bundle(request, bundle)
    scenario = next(s for s in result.scenarios if s.variant == "LEADING_DIAGONAL")
    assert scenario.parent_wave_label == "1" and scenario.parent_context == "VERIFIED"
    assert scenario.structure.status == "VERIFIED"


def test_ending_diagonal_requires_five_corrective_legs_in_confirmed_wave_c():
    def glide(start, end, length):
        return [start + (end - start) * i / length for i in range(length)]

    prices = [55, 180, 90, 185, 82, 190, 145, *glide(220, 130, 12), *glide(130, 200, 10)]
    for start, end in pairwise((200, 130, 175, 108, 145, 95)):
        prices.extend((start, start + (end - start) * 0.65, start + (end - start) * 0.37))
    prices.extend((95, 132))
    bundle = _bundle("900099.SZ", prices, per=3)
    request = AnalysisRequest(symbol="900099.SZ", as_of=bundle.as_of)
    result = AnalysisService(_settings()).analyze_bundle(request, bundle)
    scenario = next(s for s in result.scenarios if s.variant == "ENDING_DIAGONAL")
    assert scenario.parent_wave_label == "C" and scenario.parent_context == "VERIFIED"
    assert scenario.structure.status == "VERIFIED"
    assert [n.pattern_type for n in scenario.structure.nodes[1:]] == ["ZIGZAG_ABC"] * 5
