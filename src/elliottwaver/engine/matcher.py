"""Bounded search of current counts, including a separately developing last leg."""

from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import pairwise

from elliottwaver.config import SearchConfig
from elliottwaver.engine.rules import check_pattern, classify_variant
from elliottwaver.engine.templates import TEMPLATES, PatternTemplate
from elliottwaver.engine.zigzag import ZigZagResult
from elliottwaver.schemas.market import Bar
from elliottwaver.schemas.wave import PivotPoint, RuleResult, WaveScenario
from elliottwaver.util import digest


@dataclass(frozen=True)
class Candidate:
    template: PatternTemplate
    completed: tuple[PivotPoint, ...]
    tip: PivotPoint
    direction: int
    stage: str
    rules: tuple[RuleResult, ...]
    scales: tuple[float, ...]
    variant: str | None = None

    @property
    def developing(self) -> bool:
        return self.stage != "COMPLETE"

    @property
    def points(self) -> tuple[PivotPoint, ...]:
        return self.completed + (self.tip,) if self.developing else self.completed

    @property
    def key(self) -> str:
        identity = [
            self.template.name,
            self.stage,
            self.direction,
            [(p.extreme_bar_id, p.kind, p.price) for p in self.completed],
            (self.tip.extreme_bar_id, self.tip.price),
        ]
        if self.variant is not None:
            identity.append(self.variant)
        return digest(identity)[:24]


@dataclass
class SearchBudget:
    limit: int
    expansions: int = 0
    pruned: int = 0
    cache_hits: int = 0
    truncated: bool = False
    cutoffs: set[str] = field(default_factory=set)
    _bar_ids: dict[Bar, str] = field(default_factory=dict, repr=False)

    def bar_id(self, bar: Bar) -> str:
        if bar not in self._bar_ids:
            self._bar_ids[bar] = bar.bar_id
        return self._bar_ids[bar]

    def mark_limit(self, reason: str) -> None:
        self.truncated = True
        self.cutoffs.add(reason)

    def take(self) -> bool:
        if self.expansions >= self.limit:
            self.mark_limit("EXPANSION_LIMIT")
            return False
        self.expansions += 1
        return True


def deduplicate_candidates(
    candidates: Sequence[Candidate],
    *,
    tick_size: float = 0.01,
    ranked: Mapping[str, WaveScenario] | None = None,
) -> list[Candidate]:
    """Merge near-identical endpoint sets without transitive structure collapse."""

    def rank(item: Candidate) -> tuple:
        if ranked is not None:
            score = ranked[item.key].score
            return -score.quality_score, -score.coverage, item.key
        return -len(item.scales), -len(item.points), item.template.name, item.stage, item.key

    def same_invalidation(left: Candidate, right: Candidate) -> bool:
        if ranked is None:
            return True
        a, b = ranked[left.key], ranked[right.key]
        if (a.degree, a.variant) != (b.degree, b.variant):
            return False
        ca = [(c.side, c.op, c.price) for c in a.conditions if c.kind == "INVALIDATION"]
        cb = [(c.side, c.op, c.price) for c in b.conditions if c.kind == "INVALIDATION"]
        return len(ca) == len(cb) and all(
            x[:2] == y[:2] and abs(x[2] - y[2]) <= tick_size
            for x, y in zip(sorted(ca), sorted(cb), strict=True)
        )

    ordered = sorted(candidates, key=rank)
    kept: list[Candidate] = []
    for candidate in ordered:
        merged = False
        for index, representative in enumerate(kept):
            if (
                candidate.template.name != representative.template.name
                or candidate.variant != representative.variant
                or candidate.stage != representative.stage
                or candidate.direction != representative.direction
                or len(candidate.points) != len(representative.points)
            ):
                continue
            points_match = all(
                abs(left.index - right.index) <= 1
                and abs(left.price - right.price) <= tick_size
                and left.kind == right.kind
                for left, right in zip(candidate.points, representative.points, strict=True)
            )
            if points_match and same_invalidation(candidate, representative):
                kept[index] = replace(
                    representative,
                    scales=tuple(sorted(set(representative.scales + candidate.scales))),
                )
                merged = True
                break
        if not merged:
            kept.append(candidate)
    return kept


def search(
    bars: Sequence[Bar],
    zigzags: Sequence[ZigZagResult],
    anchors: Sequence[date],
    config: SearchConfig,
    *,
    exact_anchor: bool = False,
    tick_size: float = 0.01,
    advanced: bool = False,
    budget: SearchBudget | None = None,
) -> tuple[list[Candidate], SearchBudget]:
    budget = budget if budget is not None else SearchBudget(config.max_expansions)
    found: dict[str, Candidate] = {}
    extrema: dict[tuple[int, int], tuple[float, float]] = {}

    def offer(completed: tuple[PivotPoint, ...], tip: PivotPoint) -> None:
        length = len(completed)
        direction = 1 if completed[0].kind == "LOW" else -1
        for template in TEMPLATES:
            stage = (
                "COMPLETE"
                if length == template.complete_points
                else template.developing_stages.get(length)
            )
            if stage is None:
                continue
            points = completed if stage == "COMPLETE" else (*completed, tip)
            for variant in _variants(template, points, bars, direction, stage, tick_size, advanced):
                rules = check_pattern(
                    template.name,
                    points,
                    bars,
                    direction,
                    developing=stage != "COMPLETE",
                    variant=variant,
                    tick_size=tick_size,
                )
                if any(rule.status == "FAIL" for rule in rules):
                    budget.pruned += 1
                    continue
                candidate = Candidate(
                    template,
                    completed,
                    tip,
                    direction,
                    stage,
                    rules,
                    (tip.scale,),
                    variant,
                )
                key = candidate.key
                if key in found:
                    previous = found[key]
                    scales = tuple(sorted(set(previous.scales + candidate.scales)))
                    if advanced and min(p.scale for p in previous.points) > min(
                        p.scale for p in candidate.points
                    ):
                        candidate = previous
                    candidate = replace(candidate, scales=scales)
                found[key] = candidate

    def walk(zz: ZigZagResult, anchor: date) -> Iterator[None]:
        pivots = zz.confirmed
        tip = zz.tip
        if tip is None or len(pivots) < 2:
            return
        paths = [(len(pivots) - 1,)]
        for _ in range(2, 7):
            extended = []
            for path in paths:
                for gap in range(1, config.max_bridge_intervals + 1, 2):
                    previous = path[0] - gap
                    if previous < 0 or bars[pivots[previous].index].session_date < anchor:
                        continue
                    if not budget.take():
                        return
                    p, q = pivots[previous], pivots[path[0]]
                    if _valid_edge(p, q, bars, extrema, budget, advanced):
                        indices = (previous, *path)
                        extended.append(indices)
                        if not exact_anchor or bars[p.index].session_date == anchor:
                            offer(tuple(pivots[i] for i in indices), tip)
                    else:
                        budget.pruned += 1
                    yield
            extended.sort(key=lambda p: (sum(b - a - 1 for a, b in pairwise(p)), p))
            paths = extended[: config.beam_width]
            if advanced:
                starts = {path[0] for path in paths}
                for path in extended[config.beam_width :]:
                    if path[0] not in starts:
                        paths.append(path)
                        starts.add(path[0])
            if len(extended) > len(paths):
                budget.mark_limit("BEAM_LIMIT")
                budget.pruned += len(extended) - len(paths)
            if not paths:
                break

    if advanced:
        zigzags = sorted(zigzags, key=lambda item: item.tip.scale if item.tip else 0)
        anchors = sorted(set(anchors))
    branches = deque(walk(zz, anchor) for zz in zigzags for anchor in anchors)
    while branches:
        branch = branches.popleft()
        if next(branch, False) is False:
            if "EXPANSION_LIMIT" in budget.cutoffs:
                break
            continue
        if advanced:
            branches.append(branch)
        else:
            branches.appendleft(branch)
    return list(found.values()), budget


def _valid_edge(
    left: PivotPoint,
    right: PivotPoint,
    bars: Sequence[Bar],
    extrema: dict[tuple[int, int], tuple[float, float]],
    budget: SearchBudget,
    advanced: bool,
) -> bool:
    if left.kind == right.kind or right.index <= left.index:
        return False
    key = (left.index, right.index)
    measured = extrema.get(key)
    if measured is None:
        segment = bars[left.index + 1 : right.index + 1]
        if not segment:
            return False
        measured = (min(bar.low for bar in segment), max(bar.high for bar in segment))
        extrema[key] = measured
    elif advanced:
        budget.cache_hits += 1
    minimum, maximum = sorted((left.price, right.price))
    eps = max(maximum * 1e-10, 1e-10)
    return measured[0] >= minimum - eps and measured[1] <= maximum + eps


def _variants(
    template: PatternTemplate,
    points: Sequence[PivotPoint],
    bars: Sequence[Bar],
    direction: int,
    stage: str,
    tick_size: float,
    advanced: bool,
) -> tuple[str | None, ...]:
    if not advanced:
        return (None,)
    if template.name == "WXY":
        return ("DOUBLE_ZIGZAG", "DOUBLE_THREE")
    if stage != "COMPLETE":
        return (None,)
    if template.name == "IMPULSE" and direction * (points[-1].price - points[-3].price) <= 0:
        return ("TRUNCATED_FIFTH",)
    return (classify_variant(template.name, points, bars, direction, tick_size),)
