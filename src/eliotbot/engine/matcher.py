"""Bounded search of current counts, including a separately developing last leg."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise

from eliotbot.config import SearchConfig
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.templates import TEMPLATES, PatternTemplate
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import PivotPoint, RuleResult
from eliotbot.util import digest


@dataclass(frozen=True)
class Candidate:
    template: PatternTemplate
    completed: tuple[PivotPoint, ...]
    tip: PivotPoint
    direction: int
    stage: str
    rules: tuple[RuleResult, ...]
    scales: tuple[float, ...]

    @property
    def developing(self) -> bool:
        return self.stage != "COMPLETE"

    @property
    def points(self) -> tuple[PivotPoint, ...]:
        return self.completed + (self.tip,) if self.developing else self.completed

    @property
    def key(self) -> str:
        return digest(
            [
                self.template.name,
                self.stage,
                self.direction,
                [(p.extreme_bar_id, p.kind, p.price) for p in self.completed],
                (self.tip.extreme_bar_id, self.tip.price),
            ]
        )[:24]


@dataclass
class SearchBudget:
    limit: int
    expansions: int = 0
    pruned: int = 0
    truncated: bool = False

    def take(self) -> bool:
        if self.expansions >= self.limit:
            self.truncated = True
            return False
        self.expansions += 1
        return True


def search(
    bars: Sequence[Bar],
    zigzags: Sequence[ZigZagResult],
    anchors: Sequence[date],
    config: SearchConfig,
    *,
    exact_anchor: bool = False,
) -> tuple[list[Candidate], SearchBudget]:
    budget = SearchBudget(config.max_expansions)
    found: dict[str, Candidate] = {}
    for zz in zigzags:
        if zz.tip is None or len(zz.confirmed) < 2:
            continue
        pivots = zz.confirmed
        edge_cache: dict[tuple[int, int], bool] = {}

        def valid_edge(a: int, b: int, edge_cache=edge_cache, pivots=pivots) -> bool:
            key = (a, b)
            if key not in edge_cache:
                p, q = pivots[a], pivots[b]
                segment = bars[p.index + 1 : q.index + 1]
                minimum, maximum = min(p.price, q.price), max(p.price, q.price)
                eps = max(maximum * 1e-10, 1e-10)
                edge_cache[key] = (
                    p.kind != q.kind
                    and bool(segment)
                    and all(
                        bar.low >= minimum - eps and bar.high <= maximum + eps for bar in segment
                    )
                )
            return edge_cache[key]

        for anchor in anchors:
            paths = [(len(pivots) - 1,)]
            for length in range(2, 7):
                extended = []
                for path in paths:
                    for gap in range(1, config.max_bridge_intervals + 1, 2):
                        previous = path[0] - gap
                        if previous < 0 or bars[pivots[previous].index].session_date < anchor:
                            continue
                        if not budget.take():
                            return list(found.values()), budget
                        if not valid_edge(previous, path[0]):
                            budget.pruned += 1
                            continue
                        indices = (previous, *path)
                        extended.append(indices)
                        completed = tuple(pivots[i] for i in indices)
                        if exact_anchor and bars[completed[0].index].session_date != anchor:
                            continue
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
                            points = completed if stage == "COMPLETE" else (*completed, zz.tip)
                            rules = check_pattern(
                                template.name,
                                points,
                                bars,
                                direction,
                                developing=stage != "COMPLETE",
                            )
                            if any(r.status == "FAIL" for r in rules):
                                budget.pruned += 1
                                continue
                            candidate = Candidate(
                                template,
                                completed,
                                zz.tip,
                                direction,
                                stage,
                                rules,
                                (zz.tip.scale,),
                            )
                            if candidate.key in found:
                                candidate = replace(
                                    candidate,
                                    scales=tuple(
                                        sorted(set(found[candidate.key].scales + candidate.scales))
                                    ),
                                )
                            found[candidate.key] = candidate
                extended.sort(key=lambda p: (sum(b - a - 1 for a, b in pairwise(p)), p))
                if len(extended) > config.beam_width:
                    budget.truncated = True
                    budget.pruned += len(extended) - config.beam_width
                paths = extended[: config.beam_width]
                if not paths:
                    break
    return list(found.values()), budget
