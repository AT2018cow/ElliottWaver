import pytest
from conftest import make_pivots

import eliotbot.service as service_module
from eliotbot.config import Settings
from eliotbot.data.demo import demo_bundle
from eliotbot.engine.matcher import Candidate, SearchBudget
from eliotbot.engine.parents import ParentLink
from eliotbot.engine.templates import DIAGONAL, FLAT, IMPULSE, TRIANGLE
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.schemas.wave import (
    ScoreBreakdown,
    ScoreEvidence,
    StructureLeg,
    StructureNode,
    WaveStructure,
)
from eliotbot.service import AnalysisService


def _score(candidate, bars, indicators, **kwargs):
    return ScoreBreakdown(
        quality_score=80,
        fit_score=80,
        coverage=0.9,
        evidence=(
            ScoreEvidence(
                feature="fixture_fib",
                group="fibonacci",
                weight=1,
                status="AVAILABLE",
                value=1,
                fit=0.8,
                explanation="fixture",
            ),
            ScoreEvidence(
                feature="fixture_momentum",
                group="momentum_volume",
                weight=1,
                status="AVAILABLE",
                value=1,
                fit=0.8,
                explanation="fixture",
            ),
        ),
    )


def _case(template, prices, variant, parent_label):
    bars, points = make_pivots(prices)
    candidate = Candidate(
        template,
        points,
        points[-1],
        1,
        "COMPLETE",
        (),
        (1.4,),
        variant,
    )
    parent = ParentLink(
        status="VERIFIED",
        relation="EXACT_LEG",
        parent_key="parent",
        parent_pattern="IMPULSE",
        parent_degree="INTERMEDIATE",
        wave_label=parent_label,
        reason="fixture",
    )
    return bars, candidate, parent


@pytest.mark.parametrize(
    "template,prices,variant,parent_label,expected,rejected",
    [
        (
            DIAGONAL,
            [100, 120, 110, 150, 132, 160],
            "CONTRACTING_DIAGONAL",
            "1",
            "LEADING_DIAGONAL",
            False,
        ),
        (
            DIAGONAL,
            [100, 120, 110, 150, 132, 160],
            "CONTRACTING_DIAGONAL",
            "5",
            "ENDING_DIAGONAL",
            False,
        ),
        (DIAGONAL, [100, 120, 110, 150, 132, 160], "CONTRACTING_DIAGONAL", "2", None, True),
        (IMPULSE, [100, 125, 113, 152, 135, 149], "TRUNCATED_FIFTH", "5", "TRUNCATED_FIFTH", False),
        (IMPULSE, [100, 125, 113, 152, 135, 149], "TRUNCATED_FIFTH", "3", None, True),
        (FLAT, [100, 140, 101, 141], "REGULAR_FLAT", "2", "REGULAR_FLAT", False),
        (FLAT, [100, 140, 101, 141], "REGULAR_FLAT", "3", None, True),
        (
            TRIANGLE,
            [100, 120, 105, 116, 105, 111],
            "BARRIER_TRIANGLE",
            "4",
            "BARRIER_TRIANGLE",
            False,
        ),
        (TRIANGLE, [100, 120, 105, 116, 105, 111], "BARRIER_TRIANGLE", "1", None, True),
    ],
)
def test_variant_parent_position_end_to_end(
    template, prices, variant, parent_label, expected, rejected, monkeypatch
):
    bundle = demo_bundle("AAPL")
    _bars, candidate, parent = _case(template, prices, variant, parent_label)

    def fake_search(bars, zigzags, anchors, config, **kwargs):
        return [candidate], SearchBudget(config.max_expansions)

    def fake_verified_tree(candidate, bars, zigzags, *, variant, **kwargs):
        fifth = StructureLeg(
            wave_label="5", left_id="p4", right_id="p5", child_node_id="child", status="VERIFIED"
        )
        root = StructureNode(
            node_id="root",
            pattern_type=candidate.template.name,
            variant=variant,
            timeframe="1d",
            direction="UP",
            stage="COMPLETE",
            points=(),
            child_links=(fifth,),
            status="VERIFIED",
            requested_depth=1,
            verified_depth=1,
        )
        child = root.model_copy(
            update={"node_id": "child", "pattern_type": "IMPULSE", "child_links": ()}
        )
        return WaveStructure(
            root_id="root",
            status="VERIFIED",
            requested_depth=1,
            verified_depth=1,
            nodes=(root, child),
        )

    monkeypatch.setattr(service_module, "search", fake_search)
    monkeypatch.setattr(
        service_module,
        "attach_parent_context",
        lambda candidates, timeframe: {candidate.key: parent},
    )
    monkeypatch.setattr(service_module, "score", _score)
    monkeypatch.setattr(service_module, "build_structure", fake_verified_tree)
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1", "scales": (0.9, 1.4, 2.4, 3.8)},
        search={"max_subwave_depth": 1},
    )
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    result = AnalysisService(settings).analyze_bundle(request, bundle)
    if rejected:
        assert not result.scenarios
        assert any("STRUCTURES_REJECTED" in warning for warning in result.warnings)
    else:
        assert result.scenarios[0].variant == expected
