"""Rule-based subwave certification from finer-scale confirmed pivots.

The parent candidate supplies the leg endpoints.  A finer ZigZag supplies the
turning points inside each leg.  A leg is VERIFIED only when an allowed child
template (for example 5-wave motive or 3-wave corrective) passes the same hard
rules used for a top-level candidate.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import combinations, pairwise

from eliotbot.engine.matcher import Candidate
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.templates import TEMPLATES
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import PivotPoint, RuleResult, SubwaveLegEvidence, SubwaveStatus

_BY_NAME = {template.name: template for template in TEMPLATES}
_MOTIVE = ("IMPULSE", "DIAGONAL")
_CORRECTIVE = ("ZIGZAG_ABC", "FLAT")
_STATUS_RANK = {"UNAVAILABLE": 0, "UNVERIFIED": 1, "VERIFIED": 2}


@dataclass(frozen=True)
class _LegCertification:
    evidence: SubwaveLegEvidence
    points: tuple[PivotPoint, ...] = ()
    template: str | None = None
    scale: float | None = None
    rules: tuple[RuleResult, ...] = ()


@dataclass(frozen=True)
class SubwaveCertification:
    status: SubwaveStatus
    fit: float | None
    evidence: tuple[SubwaveLegEvidence, ...]
    depth: int = 0


def _leg_specs(candidate: Candidate) -> list[tuple[str, str]]:
    name = candidate.template.name
    if name in {"IMPULSE", "DIAGONAL"}:
        kinds = ("MOTIVE", "CORRECTIVE", "MOTIVE", "CORRECTIVE", "MOTIVE")
    elif name == "ZIGZAG_ABC":
        kinds = ("MOTIVE", "CORRECTIVE", "MOTIVE")
    elif name == "FLAT":
        kinds = ("CORRECTIVE", "CORRECTIVE", "MOTIVE")
    elif name == "TRIANGLE":
        kinds = ("CORRECTIVE",) * 5
    elif name == "WXY":
        kinds = ("CORRECTIVE",) * 3
    else:
        return []
    labels = candidate.template.labels
    return [
        (f"{labels[index]}->{labels[index + 1]}", kind)
        for index, kind in enumerate(kinds[: len(candidate.points) - 1])
    ]


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
    kind: str,
    bars: Sequence[Bar],
    scale: float,
    depth: int,
) -> _LegCertification:
    allowed = _MOTIVE if kind == "MOTIVE" else _CORRECTIVE
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
                return _LegCertification(evidence, points, name, scale, rules)
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
    kind: str,
    finer: Sequence[tuple[float, ZigZagResult]],
    bars: Sequence[Bar],
    depth: int,
) -> _LegCertification:
    best: _LegCertification | None = None
    for scale, zigzag in finer:
        inner = _bounded(
            [point for point in zigzag.confirmed if left.index < point.index < right.index]
        )
        result = _try_scale(leg, left, right, inner, direction, kind, bars, scale, depth)
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


def certify_subwaves(
    candidate: Candidate,
    zigzags: Sequence[ZigZagResult],
    scales: Sequence[float],
    depth: int,
    bars: Sequence[Bar],
) -> SubwaveCertification:
    """Certify child structure using scales finer than the parent candidate."""
    finer = sorted(
        (
            (scale, zigzag)
            for scale, zigzag in zip(scales, zigzags, strict=True)
            if scale < min(candidate.scales)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    cache: dict[tuple[str, tuple[float, ...], int], SubwaveCertification] = {}

    def run(current: Candidate, available: Sequence[tuple[float, ZigZagResult]], level: int):
        key = (current.key, tuple(scale for scale, _ in available), level)
        if key in cache:
            return cache[key]
        if level <= 0 or not available:
            result = SubwaveCertification("UNAVAILABLE", None, (), level)
            cache[key] = result
            return result
        specs = _leg_specs(current)
        if not specs:
            result = SubwaveCertification("UNAVAILABLE", None, (), level)
            cache[key] = result
            return result
        legs: list[_LegCertification] = []
        for index, (leg, kind) in enumerate(specs):
            left, right = current.points[index], current.points[index + 1]
            if current.developing and index == len(specs) - 1:
                legs.append(
                    _LegCertification(
                        SubwaveLegEvidence(
                            leg=leg,
                            status="UNAVAILABLE",
                            point_count=2,
                            depth=level,
                            reason="父级最后一条腿仍在进行中，端点尚未确认",
                        )
                    )
                )
                continue
            direction = 1 if right.price > left.price else -1
            legs.append(_certify_leg(leg, left, right, direction, kind, available, bars, level))
        if level > 1:
            for index, leg in enumerate(legs):
                if leg.evidence.status != "VERIFIED" or leg.template is None:
                    continue
                child = Candidate(
                    _BY_NAME[leg.template],
                    leg.points,
                    leg.points[-1],
                    1 if leg.points[-1].price > leg.points[0].price else -1,
                    "COMPLETE",
                    leg.rules,
                    (leg.scale,),
                )
                remaining = [
                    (scale, zigzag) for scale, zigzag in available if scale < (leg.scale or 0)
                ]
                nested = run(child, remaining, level - 1)
                if nested.status == "UNVERIFIED":
                    legs[index] = replace(
                        leg,
                        evidence=leg.evidence.model_copy(
                            update={
                                "status": "UNVERIFIED",
                                "reason": leg.evidence.reason + "；深度2子结构未通过",
                            }
                        ),
                    )
                elif nested.status == "VERIFIED":
                    legs[index] = replace(
                        leg,
                        evidence=leg.evidence.model_copy(
                            update={
                                "reason": leg.evidence.reason + "；深度2子结构通过",
                            }
                        ),
                    )
        result = _overall([leg.evidence for leg in legs], level)
        cache[key] = result
        return result

    return run(candidate, finer, depth)
