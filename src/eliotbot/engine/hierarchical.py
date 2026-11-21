"""Hierarchical interval certification for the opt-in advanced engine profile."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations, pairwise

from eliotbot.engine.matcher import Candidate, SearchBudget
from eliotbot.engine.rules import check_pattern, classify_variant
from eliotbot.engine.templates import (
    TEMPLATES,
    allowed_child_templates_advanced,
)
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import (
    Pattern,
    PivotPoint,
    RuleResult,
    StructureLeg,
    StructureNode,
    StructurePoint,
    StructureStatus,
    WaveStructure,
)
from eliotbot.util import digest

_BY_NAME = {template.name: template for template in TEMPLATES}


@dataclass(frozen=True)
class IntervalMatch:
    status: StructureStatus
    template: Pattern | None = None
    points: tuple[PivotPoint, ...] = ()
    rules: tuple[RuleResult, ...] = ()
    scale: float | None = None
    reason_code: str | None = None
    failed_rule_ids: tuple[str, ...] = ()
    variant: str | None = None
    structure: WaveStructure | None = None

    @property
    def verified_layers(self) -> int:
        if self.status != "VERIFIED":
            return 0
        return 1 + (self.structure.verified_depth if self.structure else 0)

    def candidate(self) -> Candidate:
        assert self.template is not None
        return Candidate(
            _BY_NAME[self.template],
            self.points,
            self.points[-1],
            1 if self.points[-1].price > self.points[0].price else -1,
            "COMPLETE",
            self.rules,
            (self.scale,) if self.scale is not None else (),
            self.variant,
        )


def is_confirmed(point: PivotPoint, as_of: datetime) -> bool:
    return (
        point.state == "CONFIRMED"
        and point.confirmed_at is not None
        and point.extreme_at <= point.confirmed_at <= as_of
    )


def _alternates(points: Sequence[PivotPoint], direction: int) -> bool:
    for index, (left, right) in enumerate(pairwise(points), 1):
        if left.kind == right.kind:
            return False
        expected_up = (index % 2 == 1) == (direction > 0)
        if expected_up != (right.price > left.price):
            return False
    return True


def match_interval(
    left: PivotPoint,
    right: PivotPoint,
    allowed: tuple[Pattern, ...],
    bars: Sequence[Bar],
    pivots_by_scale: Sequence[ZigZagResult],
    *,
    as_of: datetime,
    depth: int,
    budget: SearchBudget,
    tick_size: float = 0.01,
    coordinate_id: str = "interval",
    timeframe: str = "1d",
    raw_bars: Sequence[Bar] | None = None,
    interval_cache: dict | None = None,
) -> IntervalMatch:
    """Find a rule-valid child and prefer one that proves all requested layers."""
    if depth <= 0:
        return IntervalMatch("PENDING", reason_code="DEPTH_DISABLED")
    if not 0 <= left.index < right.index < len(bars):
        return IntervalMatch("UNRESOLVED", reason_code="EMPTY_INTERVAL")

    def observable(point: PivotPoint) -> bool:
        bar = bars[point.index]
        return (
            is_confirmed(point, as_of)
            and point.confirmed_at >= bar.usable_at
            and point.extreme_bar_id == budget.bar_id(bar)
            and point.price == getattr(bar, point.kind.lower())
        )

    if not all(observable(point) for point in (left, right)):
        return IntervalMatch("PENDING", reason_code="UNCONFIRMED_BOUNDARY")
    if any(bar.usable_at > as_of for bar in bars[left.index : right.index + 1]):
        return IntervalMatch("PENDING", reason_code="UNAVAILABLE_BAR_IN_INTERVAL")
    if not allowed:
        return IntervalMatch("UNRESOLVED", reason_code="NO_ALLOWED_TEMPLATE")
    direction = 1 if right.price > left.price else -1
    tried: set[Pattern] = set()
    failed: set[str] = set()
    best_partial: IntervalMatch | None = None
    compound_pending = False
    compound_limited = False
    scales = sorted(
        (zz for zz in pivots_by_scale if zz.confirmed),
        key=lambda zz: zz.confirmed[0].scale,
        reverse=depth > 1,
    )
    finest_scale = min((zz.confirmed[0].scale for zz in scales), default=float("inf"))
    for zigzag in scales:
        inner = sorted(
            (
                point
                for point in zigzag.confirmed
                if left.index < point.index < right.index and observable(point)
            ),
            key=lambda point: point.index,
        )
        for name in allowed:
            template = _BY_NAME.get(name)
            if template is None:
                continue
            needed = template.complete_points - 2
            if len(inner) < needed:
                continue
            for chosen in combinations(inner, needed):
                if not budget.take():
                    budget.cutoffs.add("STRUCTURE_LIMIT")
                    if best_partial:
                        return replace(best_partial, reason_code="BUDGET_EXHAUSTED")
                    return IntervalMatch(
                        "SEARCH_LIMITED",
                        template=name,
                        scale=zigzag.tip.scale if zigzag.tip is not None else None,
                        reason_code="BUDGET_EXHAUSTED",
                    )
                points = (left, *chosen, right)
                if not _alternates(points, direction):
                    continue
                tried.add(name)
                variants = (
                    ("DOUBLE_ZIGZAG", "DOUBLE_THREE")
                    if name == "WXY"
                    else (classify_variant(name, points, bars, direction, tick_size),)
                )
                for variant in variants:
                    rules = check_pattern(
                        name,
                        points,
                        bars,
                        direction,
                        developing=False,
                        variant=variant,
                        tick_size=tick_size,
                    )
                    failures = {rule.rule_id for rule in rules if rule.status == "FAIL"}
                    if failures:
                        failed.update(failures)
                        continue
                    match = IntervalMatch(
                        "VERIFIED",
                        template=name,
                        points=points,
                        rules=rules,
                        scale=chosen[0].scale,
                        variant=variant,
                    )
                    child_depth = max(depth - 1, int(name == "WXY"))
                    if child_depth == 0:
                        return match
                    if name == "WXY" and finest_scale >= min(p.scale for p in points):
                        compound_pending = True
                        continue
                    proof = build_structure(
                        match.candidate(),
                        bars,
                        scales,
                        as_of=as_of,
                        coordinate_id=coordinate_id,
                        depth=child_depth,
                        budget=budget,
                        timeframe=timeframe,
                        variant=variant,
                        raw_bars=raw_bars,
                        tick_size=tick_size,
                        interval_cache=interval_cache,
                    )
                    if name == "WXY" and proof.verified_depth < 1:
                        compound_limited |= proof.status == "SEARCH_LIMITED"
                        compound_pending = True
                        continue
                    match = replace(match, structure=proof)
                    if match.verified_layers >= depth:
                        return match
                    if best_partial is None or _match_strength(match) > _match_strength(
                        best_partial
                    ):
                        best_partial = match
    if best_partial is not None:
        return (
            replace(best_partial, reason_code="BUDGET_EXHAUSTED")
            if compound_limited
            else best_partial
        )
    if compound_limited:
        return IntervalMatch("SEARCH_LIMITED", reason_code="BUDGET_EXHAUSTED")
    if compound_pending:
        return IntervalMatch("PENDING", reason_code="WXY_SUBDIVISION_UNVERIFIED")
    if not tried:
        return IntervalMatch("PENDING", reason_code="INSUFFICIENT_FINER_PIVOTS")
    if tried != set(allowed):
        return IntervalMatch("UNRESOLVED", reason_code="INCOMPLETE_TEMPLATE_COVERAGE")
    return IntervalMatch(
        "REJECTED",
        reason_code="ALL_ALLOWED_STRUCTURES_FAILED",
        failed_rule_ids=tuple(sorted(failed)),
    )


def _match_strength(match: IntervalMatch) -> tuple[int, bool]:
    return match.verified_layers, match.structure is None or match.structure.status != "REJECTED"


def _search_limited(match: IntervalMatch) -> bool:
    return (
        match.status == "SEARCH_LIMITED"
        or match.reason_code == "BUDGET_EXHAUSTED"
        or (match.structure is not None and match.structure.status == "SEARCH_LIMITED")
    )


def _point(
    point: PivotPoint, bars: Sequence[Bar], coordinate_id: str, budget: SearchBudget
) -> StructurePoint:
    bar_id = (
        budget.bar_id(bars[point.index]) if 0 <= point.index < len(bars) else point.extreme_bar_id
    )
    return StructurePoint(
        point_id=digest(
            [point.pivot_id, point.extreme_bar_id, point.kind, point.price, point.index]
        )[:24],
        pivot=point,
        bar_id=bar_id,
        price_coordinate_id=coordinate_id,
    )


def _node_id(candidate: Candidate, coordinate_id: str, variant: str | None) -> str:
    return digest(
        [
            "hierarchical-node",
            candidate.template.name,
            candidate.stage,
            candidate.direction,
            variant,
            [
                (point.extreme_bar_id, point.kind, point.price, point.index)
                for point in candidate.points
            ],
            coordinate_id,
        ]
    )[:24]


def structure_variant(structure: WaveStructure, pattern: str) -> str | None:
    if pattern != "WXY" or not structure.nodes or structure.verified_depth < 1:
        return None
    root = structure.nodes[0]
    if root.variant in {"DOUBLE_ZIGZAG", "DOUBLE_THREE"}:
        return root.variant
    nodes = {node.node_id: node for node in structure.nodes}
    children = {link.wave_label: nodes.get(link.child_node_id) for link in root.child_links}
    w, y = children.get("W"), children.get("Y")
    if w is None or y is None:
        return None
    if w.pattern_type == y.pattern_type == "ZIGZAG_ABC":
        return "DOUBLE_ZIGZAG"
    return "DOUBLE_THREE"


def _profiles(
    candidate: Candidate, variant: str | None
) -> tuple[tuple[tuple[Pattern, ...], ...], ...]:
    pattern = candidate.template.name
    base = tuple(
        allowed_child_templates_advanced(pattern, index, variant=variant)
        for index in range(len(candidate.points) - 1)
    )
    if variant == "LEADING_DIAGONAL":
        threes = allowed_child_templates_advanced("DIAGONAL", 0, variant="ENDING_DIAGONAL")
        fives = ("IMPULSE", "DIAGONAL")
        return (
            tuple(threes for _ in base),
            tuple(fives if index % 2 == 0 else threes for index in range(len(base))),
        )
    if variant == "DOUBLE_THREE" and len(base) == 3:
        return (
            (("FLAT",), base[1], base[2]),
            (("ZIGZAG_ABC",), base[1], ("FLAT", "TRIANGLE")),
        )
    contracting = pattern == "TRIANGLE" and any(
        rule.rule_id == "TRIANGLE_SHAPE"
        and rule.status == "PASS"
        and rule.observed is not None
        and rule.boundary is not None
        and rule.observed < rule.boundary
        for rule in candidate.rules
    )
    if contracting and len(base) == 5:
        zigzag = ("ZIGZAG_ABC",)
        return (
            (zigzag,) * 5,
            *(
                tuple(("FLAT", "WXY") if leg == complex_leg else zigzag for leg in range(5))
                for complex_leg in range(5)
            ),
        )
    return (base,)


def build_structure(
    candidate: Candidate,
    bars: Sequence[Bar],
    zigzags: Sequence[ZigZagResult],
    *,
    as_of: datetime,
    coordinate_id: str,
    depth: int,
    budget: SearchBudget,
    timeframe: str = "1d",
    variant: str | None = None,
    raw_bars: Sequence[Bar] | None = None,
    tick_size: float = 0.01,
    interval_cache: dict | None = None,
) -> WaveStructure:
    """Build a tree with an explicit witness for every verified direct leg."""
    evidence_bars = raw_bars if raw_bars is not None else bars
    cache = interval_cache if interval_cache is not None else {}

    def node(
        item: Candidate,
        subtype: str | None,
        requested: int,
        status: StructureStatus,
        verified: int,
        links: tuple[StructureLeg, ...] = (),
        reasons: tuple[str, ...] = (),
    ) -> StructureNode:
        return StructureNode(
            node_id=_node_id(item, coordinate_id, subtype),
            pattern_type=item.template.name,
            variant=subtype,
            timeframe=timeframe,
            direction="UP" if item.direction > 0 else "DOWN",
            stage=item.stage,
            points=tuple(
                _point(point, evidence_bars, coordinate_id, budget) for point in item.points
            ),
            rules=item.rules,
            child_links=links,
            status=status,
            requested_depth=requested,
            verified_depth=verified,
            reasons=reasons,
        )

    def visit(item: Candidate, subtype: str | None, level: int) -> tuple[StructureNode, ...]:
        threshold = min(p.scale for p in item.points)
        finer = tuple(zz for zz in zigzags if zz.confirmed and zz.confirmed[0].scale < threshold)
        scale_key = tuple(zz.confirmed[0].scale for zz in finer)
        possibilities = []
        for profile in _profiles(item, subtype):
            matches = []
            for index, ((left, right), allowed) in enumerate(
                zip(pairwise(item.points), profile, strict=True)
            ):
                if item.developing and index == len(profile) - 1:
                    matches.append(IntervalMatch("PENDING", reason_code="DEVELOPING_LEG"))
                    continue
                key = (left, right, allowed, scale_key, level)
                if key not in cache:
                    match = match_interval(
                        left,
                        right,
                        allowed,
                        bars,
                        finer,
                        as_of=as_of,
                        depth=level,
                        budget=budget,
                        tick_size=tick_size,
                        coordinate_id=coordinate_id,
                        timeframe=timeframe,
                        raw_bars=raw_bars,
                        interval_cache=cache,
                    )
                    if not _search_limited(match):
                        cache[key] = match
                else:
                    budget.cache_hits += 1
                    match = cache[key]
                matches.append(match)
            possibilities.append((profile, matches))
            if all(match.verified_layers >= level for match in matches):
                break
        all_rejected = all(any(m.status == "REJECTED" for m in row) for _, row in possibilities)
        profile, matches = max(
            possibilities,
            key=lambda item: (
                all(m.status != "REJECTED" for m in item[1]),
                min((m.verified_layers for m in item[1]), default=0),
                sum(m.status == "VERIFIED" for m in item[1]),
                sum(m.verified_layers for m in item[1]),
            ),
        )
        points = tuple(_point(point, evidence_bars, coordinate_id, budget) for point in item.points)
        links = []
        children = []
        for index, (allowed, match) in enumerate(zip(profile, matches, strict=True)):
            child_id = None
            if match.status == "VERIFIED":
                child_tree = (
                    match.structure.nodes
                    if match.structure is not None
                    else (node(match.candidate(), match.variant, 0, "VERIFIED", 0),)
                )
                children.extend(child_tree)
                child_id = child_tree[0].node_id
            links.append(
                StructureLeg(
                    wave_label=item.template.labels[index + 1],
                    left_id=points[index].point_id,
                    right_id=points[index + 1].point_id,
                    allowed_templates=allowed,
                    child_node_id=child_id,
                    status=match.status,
                    reason_code=match.reason_code,
                    failed_rule_ids=match.failed_rule_ids,
                )
            )

        verified = min(level, min((match.verified_layers for match in matches), default=0))
        limited = any(_search_limited(m) for _, row in possibilities for m in row)
        if verified == level:
            status = "VERIFIED"
        elif all_rejected:
            status = "REJECTED"
        elif limited:
            status = "SEARCH_LIMITED"
        elif any(match.status == "UNRESOLVED" for match in matches):
            status = "UNRESOLVED"
        else:
            status = "PENDING"
        reasons = tuple(f"{leg.wave_label}:{leg.reason_code}" for leg in links if leg.reason_code)
        if verified and verified < level:
            reasons += ("DEEPER_EVIDENCE_INCOMPLETE",)
        root = node(item, subtype, level, status, verified, tuple(links), reasons)
        return (root, *children)

    nodes = (
        visit(candidate, variant, depth)
        if depth > 0
        else (node(candidate, variant, 0, "PENDING", 0, reasons=("DEPTH_DISABLED",)),)
    )
    root = nodes[0]
    return WaveStructure(
        root_id=root.node_id,
        status=root.status,
        requested_depth=depth,
        verified_depth=root.verified_depth,
        nodes=nodes,
        reasons=root.reasons,
    )
