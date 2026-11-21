"""Rule-based subwave certification from finer-scale confirmed pivots.

The parent candidate supplies the leg endpoints.  A finer ZigZag supplies the
turning points inside each leg.  A leg is VERIFIED only when an allowed child
template (for example 5-wave motive or 3-wave corrective) passes the same hard
rules used for a top-level candidate.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import combinations, pairwise, product

from eliotbot.engine.matcher import Candidate
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.templates import (
    TEMPLATES,
    allowed_child_templates,
    allowed_child_templates_advanced,
)
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import Pattern, PivotPoint, RuleResult, SubwaveLegEvidence, SubwaveStatus

_BY_NAME = {template.name: template for template in TEMPLATES}
# The bounded fine-pivot search certifies only these four child templates.
_CERTIFIABLE = {"IMPULSE", "DIAGONAL", "ZIGZAG_ABC", "FLAT"}
_STATUS_RANK = {"UNAVAILABLE": 0, "UNVERIFIED": 1, "VERIFIED": 2}


@dataclass(frozen=True)
class _LegCertification:
    evidence: SubwaveLegEvidence
    points: tuple[PivotPoint, ...] = ()
    template: Pattern | None = None
    scale: float | None = None
    rules: tuple[RuleResult, ...] = ()


@dataclass(frozen=True)
class SubwaveCertification:
    status: SubwaveStatus
    fit: float | None
    evidence: tuple[SubwaveLegEvidence, ...]
    depth: int = 0


def _bounded(pivots: Sequence[PivotPoint], limit: int = 12) -> tuple[PivotPoint, ...]:
    if len(pivots) <= limit:
        return tuple(pivots)
    step = (len(pivots) - 1) / (limit - 1)
    indices = sorted({round(index * step) for index in range(limit)})
    return tuple(pivots[index] for index in indices)


def _plausible(points: Sequence[PivotPoint], direction: int) -> bool:
    for index, (left, right) in enumerate(pairwise(points), 1):
        if left.kind == right.kind:
            return False
        going_up = (index % 2 == 1) == (direction > 0)
        if going_up != (right.price > left.price):
            return False
    return True


def _try_scale(
    leg: str,
    left: PivotPoint,
    right: PivotPoint,
    inner: Sequence[PivotPoint],
    direction: int,
    allowed: Sequence[Pattern],
    bars: Sequence[Bar],
    scale: float,
    depth: int,
    refine: Callable[[_LegCertification], _LegCertification] | None = None,
) -> _LegCertification:
    tried = False
    for name in allowed:
        template = _BY_NAME[name]
        needed = template.complete_points - 2
        if len(inner) < needed:
            continue
        tried = True
        for chosen in combinations(inner, needed):
            points = (left, *chosen, right)
            if not _plausible(points, direction):
                continue
            rules = check_pattern(name, points, bars, direction, developing=False)
            if not any(rule.status == "FAIL" for rule in rules):
                evidence = SubwaveLegEvidence(
                    leg=leg,
                    status="VERIFIED",
                    pattern=name,
                    point_count=len(points),
                    scale=scale,
                    depth=depth,
                    reason=f"{name} 子结构通过硬规则",
                )
                match = _LegCertification(evidence, points, name, scale, rules)
                if refine is not None:
                    match = refine(match)
                if match.evidence.status == "VERIFIED":
                    return match
    if tried:
        return _LegCertification(
            SubwaveLegEvidence(
                leg=leg,
                status="UNVERIFIED",
                point_count=len(inner) + 2,
                scale=scale,
                depth=depth,
                reason="存在更细拐点，但没有允许的子模板通过硬规则",
            )
        )
    return _LegCertification(
        SubwaveLegEvidence(
            leg=leg,
            status="UNAVAILABLE",
            point_count=len(inner) + 2,
            scale=scale,
            depth=depth,
            reason="更细尺度没有足够的交替拐点",
        )
    )


def _certify_leg(
    leg: str,
    left: PivotPoint,
    right: PivotPoint,
    direction: int,
    allowed: Sequence[Pattern],
    finer: Sequence[tuple[float, ZigZagResult]],
    bars: Sequence[Bar],
    depth: int,
    refine: Callable[[_LegCertification], _LegCertification] | None = None,
) -> _LegCertification:
    best: _LegCertification | None = None
    for scale, zigzag in finer:
        inner = _bounded(
            [point for point in zigzag.confirmed if left.index < point.index < right.index]
        )
        result = _try_scale(leg, left, right, inner, direction, allowed, bars, scale, depth, refine)
        if (
            best is None
            or _STATUS_RANK[result.evidence.status] > _STATUS_RANK[best.evidence.status]
        ):
            best = result
        if result.evidence.status == "VERIFIED":
            break
    return best or _LegCertification(
        SubwaveLegEvidence(
            leg=leg,
            status="UNAVAILABLE",
            depth=depth,
            reason="没有可用的更细尺度数据",
        )
    )


def _overall(evidence: Sequence[SubwaveLegEvidence], depth: int) -> SubwaveCertification:
    statuses = [item.status for item in evidence]
    if statuses and all(status == "VERIFIED" for status in statuses):
        status: SubwaveStatus = "VERIFIED"
    elif any(status == "UNVERIFIED" for status in statuses):
        status = "UNVERIFIED"
    elif any(status == "VERIFIED" for status in statuses):
        status = "UNVERIFIED"
    else:
        status = "UNAVAILABLE"
    verified = sum(status == "VERIFIED" for status in statuses)
    fit = None if status == "UNAVAILABLE" else verified / len(statuses)
    return SubwaveCertification(status, fit, tuple(evidence), depth)


def is_completed_contracting_triangle(candidate: Candidate) -> bool:
    if candidate.template.name != "TRIANGLE" or candidate.stage != "COMPLETE":
        return False
    shape = next((rule for rule in candidate.rules if rule.rule_id == "TRIANGLE_SHAPE"), None)
    return (
        shape is not None
        and shape.status == "PASS"
        and shape.observed is not None
        and shape.boundary is not None
        and shape.observed < shape.boundary
    )


def classify_triangle_legs(evidence: Sequence[SubwaveLegEvidence]) -> dict[str, object]:
    """Classify a triangle's legs under the Frost/Prechter group rule.

    A zigzag-family leg is ZIGZAG_ABC or WXY. A complex leg is FLAT, WXY or
    TRIANGLE; WXY therefore counts in both categories, which prevents two
    WXY legs from being mistaken for zero complex legs.
    """
    patterns = [item.pattern for item in evidence if item.pattern is not None]
    all_verified = len(evidence) == 5 and all(item.status == "VERIFIED" for item in evidence)
    zigzag_legs = sum(pattern in {"ZIGZAG_ABC", "WXY"} for pattern in patterns)
    complex_legs = sum(pattern in {"FLAT", "WXY", "TRIANGLE"} for pattern in patterns)
    return {
        "all_verified": all_verified,
        "zigzag_legs": zigzag_legs,
        "complex_legs": complex_legs,
        "classic": all_verified and zigzag_legs >= 4 and complex_legs <= 1,
    }


def certify_subwaves(
    candidate: Candidate,
    zigzags: Sequence[ZigZagResult],
    scales: Sequence[float],
    depth: int,
    bars: Sequence[Bar],
    *,
    classic_triangles: bool = True,
    advanced: bool = False,
    variant: str | None = None,
) -> SubwaveCertification:
    """Certify thinner-scale structure, with the classic triangle rule by default.

    The loose option preserves per-leg selection for the controlled experiment.
    ``advanced`` uses the wider WXY/diagonal child permissions of the
    hierarchical profile; ``variant`` selects the leading or ending diagonal
    internal subdivision.
    """
    # Deduplicated candidates merge the scales that produced identical endpoints.
    # The child search must start below the pivots' own scale, not below the
    # smallest scale that happened to reproduce the same candidate.
    point_scales = [point.scale for point in candidate.points if point.scale is not None]
    base_scale = min(point_scales) if point_scales else min(candidate.scales)
    finer = sorted(
        (
            (scale, zigzag)
            for scale, zigzag in zip(scales, zigzags, strict=True)
            if scale < base_scale
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    cache: dict[tuple[str, tuple[float, ...], int], SubwaveCertification] = {}

    def run(
        current: Candidate, available: Sequence[tuple[float, ZigZagResult]], level: int
    ) -> SubwaveCertification:
        key = (current.key, tuple(scale for scale, _ in available), level)
        if key in cache:
            return cache[key]
        if level <= 0 or not available:
            result = SubwaveCertification("UNAVAILABLE", None, (), level)
            cache[key] = result
            return result
        labels = current.template.labels
        points = current.points

        def allowed_at(index: int) -> tuple[Pattern, ...]:
            allowed = (
                allowed_child_templates_advanced(current.template.name, index, variant=variant)
                if advanced
                else allowed_child_templates(current.template.name, index)
            )
            return tuple(name for name in allowed if name in _CERTIFIABLE)

        def deepen(leg: _LegCertification) -> _LegCertification:
            if leg.evidence.status != "VERIFIED" or leg.template is None or leg.scale is None:
                return leg
            child = Candidate(
                _BY_NAME[leg.template],
                leg.points,
                leg.points[-1],
                1 if leg.points[-1].price > leg.points[0].price else -1,
                "COMPLETE",
                leg.rules,
                (leg.scale,),
            )
            remaining = [(scale, zigzag) for scale, zigzag in available if scale < leg.scale]
            nested = run(child, remaining, level - 1)
            if nested.status == "UNAVAILABLE":
                return leg
            reason = "；深度2子结构通过" if nested.status == "VERIFIED" else "；深度2子结构未通过"
            return replace(
                leg,
                evidence=leg.evidence.model_copy(
                    update={"status": nested.status, "reason": leg.evidence.reason + reason}
                ),
            )

        legs: list[_LegCertification] = []
        for index, (left, right) in enumerate(pairwise(points)):
            leg = f"{labels[index]}->{labels[index + 1]}"
            allowed = allowed_at(index)
            unavailable_reason = None
            if current.developing and index == len(points) - 2:
                unavailable_reason = "父级最后一条腿仍在进行中，端点尚未确认"
            elif not allowed:
                unavailable_reason = "该父浪位置没有可用的内部结构认证规则"
            if unavailable_reason:
                legs.append(
                    _LegCertification(
                        SubwaveLegEvidence(
                            leg=leg,
                            status="UNAVAILABLE",
                            point_count=2,
                            depth=level,
                            reason=unavailable_reason,
                        )
                    )
                )
                continue
            direction = 1 if right.price > left.price else -1
            legs.append(_certify_leg(leg, left, right, direction, allowed, available, bars, level))
        resolved = [deepen(leg) for leg in legs] if level > 1 else legs
        result = _overall([leg.evidence for leg in resolved], level)
        if classic_triangles and is_completed_contracting_triangle(current):
            policy = classify_triangle_legs(result.evidence)
            if not policy["classic"] and all(leg.evidence.status == "VERIFIED" for leg in legs):
                # The quota depends on template names. One verified match per
                # template suffices, even when the match comes from a finer scale.
                options: list[list[SubwaveLegEvidence]] = []
                for index, ((left, right), first) in enumerate(
                    zip(pairwise(points), resolved, strict=True)
                ):
                    choices = [first.evidence] if first.evidence.status == "VERIFIED" else []
                    direction = 1 if right.price > left.price else -1
                    for name in allowed_at(index):
                        if choices and name == first.evidence.pattern:
                            continue
                        alternate = _certify_leg(
                            first.evidence.leg,
                            left,
                            right,
                            direction,
                            (name,),
                            available,
                            bars,
                            level,
                            refine=deepen if level > 1 else None,
                        )
                        if alternate.evidence.status == "VERIFIED":
                            choices.append(alternate.evidence)
                    if not choices:
                        break
                    options.append(choices)
                if len(options) == len(legs):
                    for evidence in product(*options):
                        if classify_triangle_legs(evidence)["classic"]:
                            result = _overall(evidence, level)
                            break
                if (
                    result.status == "VERIFIED"
                    and not classify_triangle_legs(result.evidence)["classic"]
                ):
                    result = replace(result, status="UNVERIFIED", fit=0.0)
        cache[key] = result
        return result

    return run(candidate, finer, depth)
