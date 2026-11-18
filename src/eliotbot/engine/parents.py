"""Containment-based parent context for candidate wave structures."""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from eliotbot.engine.degree import wave_degree
from eliotbot.engine.matcher import Candidate
from eliotbot.schemas.wave import Degree, ParentContext, ParentRelation, Pattern

_DEGREE_RANK: dict[Degree, int] = {
    "UNKNOWN": 0,
    "MINOR": 1,
    "INTERMEDIATE": 2,
    "PRIMARY": 3,
    "CYCLE": 4,
    "SUPERCYCLE": 5,
}


@dataclass(frozen=True)
class ParentLink:
    status: ParentContext = "UNKNOWN"
    relation: ParentRelation = "UNKNOWN"
    parent_key: str | None = None
    parent_pattern: Pattern | None = None
    parent_degree: Degree | None = None
    wave_label: str | None = None
    leg_span: int | None = None
    reason: str = ""


def _better(candidate: ParentLink, current: ParentLink | None) -> bool:
    if current is None:
        return True
    candidate_rank = _DEGREE_RANK[candidate.parent_degree or "UNKNOWN"]
    current_rank = _DEGREE_RANK[current.parent_degree or "UNKNOWN"]
    return (
        candidate.relation == "EXACT_LEG",
        candidate.status == "VERIFIED",
        -(candidate.leg_span or 0),
        candidate_rank,
        candidate.parent_key or "",
    ) > (
        current.relation == "EXACT_LEG",
        current.status == "VERIFIED",
        -(current.leg_span or 0),
        current_rank,
        current.parent_key or "",
    )


def attach_parent_context(candidates: Sequence[Candidate], timeframe: str) -> dict[str, ParentLink]:
    """Link each candidate to the narrowest higher-degree parent leg containing it.

    A link is only VERIFIED when the child is fully inside one parent leg and the
    child direction matches that leg.  A child touching the developing last leg
    of a parent is marked PROVISIONAL because that endpoint can still move.
    """
    degrees = {candidate.key: wave_degree(candidate.points, timeframe) for candidate in candidates}
    links: dict[str, ParentLink] = {}
    for child in candidates:
        child_degree = degrees[child.key]
        best: ParentLink | None = None
        for parent in candidates:
            if parent.key == child.key:
                continue
            parent_degree = degrees[parent.key]
            if _DEGREE_RANK[parent_degree] <= _DEGREE_RANK[child_degree]:
                continue
            parent_points = parent.points
            for leg_index, (left, right) in enumerate(pairwise(parent_points)):
                if left.index > child.points[0].index or child.points[-1].index > right.index:
                    continue
                leg_direction = 1 if right.price > left.price else -1
                if child.direction != leg_direction:
                    continue
                exact = (
                    left.index == child.points[0].index and right.index == child.points[-1].index
                )
                provisional = parent.developing and leg_index == len(parent_points) - 2
                label = parent.template.labels[leg_index + 1]
                relation = "EXACT_LEG" if exact else "CONTAINED"
                status = "PROVISIONAL" if provisional else "VERIFIED"
                reason = (
                    f"{'matches' if exact else 'contained in'} {parent.template.name} "
                    f"wave {label} ({parent.key})"
                )
                if provisional:
                    reason += "; parent leg is still developing"
                link = ParentLink(
                    status=status,
                    relation=relation,
                    parent_key=parent.key,
                    parent_pattern=parent.template.name,
                    parent_degree=parent_degree,
                    wave_label=label,
                    leg_span=right.index - left.index,
                    reason=reason,
                )
                if _better(link, best):
                    best = link
        links[child.key] = best or ParentLink()
    return links
