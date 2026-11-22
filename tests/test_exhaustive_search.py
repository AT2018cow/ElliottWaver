import json

from conftest import make_pivots

from elliottwaver.config import SearchConfig
from elliottwaver.engine.matcher import Candidate, search
from elliottwaver.engine.rules import check_pattern
from elliottwaver.engine.templates import TEMPLATES
from elliottwaver.engine.zigzag import ZigZagResult


def test_advanced_budget_reaches_alternate_scale_regardless_of_input_order():
    bars, pivots = make_pivots([100, 125, 113, 152, 135])

    def zigzag(start, scale):
        points = tuple(p.model_copy(update={"scale": scale}) for p in pivots[start : start + 3])
        return ZigZagResult(points[:2], points[-1])

    a, b = zigzag(0, 1.4), zigzag(2, 3.8)
    keys = []
    for scales in ((a, b), (b, a)):
        matches, budget = search(
            bars,
            scales,
            [bars[0].session_date],
            SearchConfig(max_expansions=2),
            advanced=True,
        )
        assert budget.expansions == 2
        assert any(c.completed[0].price == 113 for c in matches)
        keys.append(tuple(c.key for c in matches))
    assert keys[0] == keys[1]


def _reference(bars, points, tip, anchor, max_bridge=5):
    found = {}

    def valid_edge(a, b):
        left, right = points[a], points[b]
        segment = bars[left.index + 1 : right.index + 1]
        low, high = min(left.price, right.price), max(left.price, right.price)
        eps = max(high * 1e-10, 1e-10)
        return (
            left.kind != right.kind
            and bool(segment)
            and all(bar.low >= low - eps and bar.high <= high + eps for bar in segment)
        )

    paths = [(len(points) - 1,)]
    for length in range(2, 7):
        extended = []
        for path in paths:
            for gap in range(1, max_bridge + 1, 2):
                previous = path[0] - gap
                if previous < 0 or bars[points[previous].index].session_date < anchor:
                    continue
                if not valid_edge(previous, path[0]):
                    continue
                indices = (previous, *path)
                extended.append(indices)
                completed = tuple(points[index] for index in indices)
                direction = 1 if completed[0].kind == "LOW" else -1
                for template in TEMPLATES:
                    if length > template.complete_points:
                        continue
                    stage = (
                        "COMPLETE"
                        if length == template.complete_points
                        else template.developing_stages.get(length)
                    )
                    if stage is None:
                        continue
                    candidate_points = completed if stage == "COMPLETE" else (*completed, tip)
                    rules = check_pattern(
                        template.name,
                        candidate_points,
                        bars,
                        direction,
                        developing=stage != "COMPLETE",
                    )
                    if any(rule.status == "FAIL" for rule in rules):
                        continue
                    candidate = Candidate(
                        template,
                        completed,
                        tip,
                        direction,
                        stage,
                        rules,
                        (tip.scale,),
                    )
                    found[candidate.key] = candidate
        paths = extended
    return found


def test_small_search_matches_independent_exhaustive_reference():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160])
    anchor = bars[points[0].index].session_date
    config = SearchConfig(beam_width=1000, max_expansions=100000, max_bridge_intervals=5)
    found, budget = search(
        bars,
        [ZigZagResult(points, points[-1])],
        [anchor],
        config,
        tick_size=0.01,
    )
    assert not budget.truncated
    assert {candidate.key for candidate in found} == set(
        _reference(bars, points, points[-1], anchor, 5)
    )


def test_eight_pivot_search_matches_independent_exhaustive_reference():
    bars, points = make_pivots([100, 120, 110, 150, 132, 160, 145, 175])
    anchor = bars[points[0].index].session_date
    config = SearchConfig(beam_width=1024, max_expansions=200000, max_bridge_intervals=7)
    found, budget = search(
        bars,
        [ZigZagResult(points, points[-1])],
        [anchor],
        config,
        tick_size=0.01,
    )
    assert not budget.truncated
    assert {candidate.key for candidate in found} == set(
        _reference(bars, points, points[-1], anchor, 7)
    )


def test_fourteen_pivot_search_matches_independent_exhaustive_reference():
    bars, pivots = make_pivots(
        [100, 120, 110, 150, 132, 160, 145, 175, 158, 190, 172, 205, 188, 220, 204],
        developing=True,
    )
    points, tip = pivots[:-1], pivots[-1]
    anchor = bars[points[0].index].session_date
    config = SearchConfig(beam_width=1024, max_expansions=1000000, max_bridge_intervals=13)
    found, budget = search(
        bars,
        [ZigZagResult(points, tip)],
        [anchor],
        config,
        tick_size=0.01,
    )
    assert not budget.truncated
    reference = _reference(bars, points, tip, anchor, 13)
    assert any(candidate.developing for candidate in reference.values())
    assert {candidate.key for candidate in found} == set(reference)


def test_recall_report_counts_comparable_pairs_and_developing_candidates(tmp_path):
    from scripts.compare_candidate_recall import _exhaustive_recall, _run_row

    def cohort(switches, pairs):
        return {
            "analyzable": pairs + 1,
            "structure": {
                "no_signal": 0,
                "search_truncated": 0,
                "primary_switches": switches,
                "comparable_primary_pairs": pairs,
                "primary_switch_rate": switches / pairs if pairs else None,
            },
        }

    summary = tmp_path / "summary.json"
    (tmp_path / "decisions.jsonl").write_text("")
    for cohorts, expected in (
        ({"EQUITY": cohort(2, 8), "INDEX": cohort(1, 2), "ETF": cohort(0, 0)}, 0.3),
        ({"EQUITY": cohort(0, 0)}, None),
    ):
        summary.write_text(
            json.dumps({"decisions": 10, "analyzable": 10, "by_asset_type": cohorts})
        )
        assert (
            _run_row({"engine_profile": "classic_v4"}, None, tmp_path)["primary_switch_rate"]
            == expected
        )

    config = SearchConfig(beam_width=1024, max_expansions=1000000, max_bridge_intervals=13)
    for advanced in (False, True):
        result = _exhaustive_recall(config, advanced=advanced)
        assert result["reference_developing_candidates"] > 0
        assert result["matched_developing_candidates"] == result["reference_developing_candidates"]
