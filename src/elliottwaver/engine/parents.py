"""Parent context from candidate geometry and wave-position rules."""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from elliottwaver.engine.degree import wave_degree
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.templates import allowed_child_templates
from elliottwaver.schemas.wave import Degree, ParentContext, ParentRelation, Pattern

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
        candidate.status == "VERIFIED",
        candidate.relation == "EXACT_LEG",
        -(candidate.leg_span or 0),
        candidate_rank,
        candidate.parent_key or "",
    ) > (
        current.status == "VERIFIED",
        current.relation == "EXACT_LEG",
        -(current.leg_span or 0),
        current_rank,
        current.parent_key or "",
    )


def attach_parent_context(candidates: Sequence[Candidate], timeframe: str) -> dict[str, ParentLink]:
    """Prefer exact, position-compatible matches to higher-degree parent legs."""
    degrees = {candidate.key: wave_degree(candidate.points, timeframe) for candidate in candidates}
    links: dict[str, ParentLink] = {}
    for child in candidates:
        child_degree = degrees[child.key]
        child_start, child_end = child.points[0], child.points[-1]
        best: ParentLink | None = None
        for parent in candidates:
            if parent.key == child.key:
                continue
            parent_degree = degrees[parent.key]
            if _DEGREE_RANK[parent_degree] <= _DEGREE_RANK[child_degree]:
                continue
            parent_points = parent.points
            for leg_index, (left, right) in enumerate(pairwise(parent_points)):
                if left.index > child_start.index or child_end.index > right.index:
                    continue
                leg_direction = 1 if right.price > left.price else -1
                if child.direction != leg_direction:
                    continue
                exact = left.index == child_start.index and right.index == child_end.index
                allowed = allowed_child_templates(parent.template.name, leg_index)
                parent_leg_developing = parent.developing and leg_index == len(parent_points) - 2
                if exact and (
                    (left.price, left.kind) != (child_start.price, child_start.kind)
                    or (right.price, right.kind) != (child_end.price, child_end.kind)
                    or (allowed and child.template.name not in allowed)
                ):
                    continue
                label = parent.template.labels[leg_index + 1]
                relation = "EXACT_LEG" if exact else "CONTAINED"
                nested_e = (
                    parent.template.name == "TRIANGLE"
                    and leg_index == 4
                    and child.template.name == "TRIANGLE"
                )
                status: ParentContext = "PROVISIONAL"
                if (
                    not nested_e
                    and exact
                    and allowed
                    and not parent_leg_developing
                    and not child.developing
                ):
                    status = "VERIFIED"
                reason = (
                    f"{'matches' if exact else 'contained in'} {parent.template.name} "
                    f"wave {label} ({parent.key})"
                )
                if nested_e:
                    reason += " (nested E-leg triangle is a provisional candidate)"
                elif not exact:
                    reason += " (time containment only)"
                elif parent_leg_developing or child.developing:
                    reason += " (parent leg or child is still developing)"
                elif not allowed:
                    reason += " (parent subdivision profile unavailable)"
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
