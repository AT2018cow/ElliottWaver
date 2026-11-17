"""Conditional targets subject to the same hard boundaries as the count."""

from collections.abc import Sequence
from datetime import datetime

from eliotbot.engine.matcher import Candidate
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import LevelCondition, PriceZone, ProjectedPoint


def project(
    candidate: Candidate,
    bars: Sequence[Bar],
    atr: float,
    coordinate_id: str,
    as_of: datetime,
    tick: float,
    timeframe: str = "1d",
) -> tuple[tuple[LevelCondition, ...], tuple[PriceZone, ...], tuple[ProjectedPoint, ...]]:
    points = candidate.points
    p = [x.price for x in points]
    s = candidate.direction
    current = bars[-1].close
    conditions: list[LevelCondition] = []
    targets: list[tuple[float, str]] = []
    cap: float | None = None
    target_direction = s

    def condition(kind, price, upward, reason, inclusive=True):
        if price <= 0:
            return
        conditions.append(
            LevelCondition(
                kind=kind,
                side="HIGH" if upward else "LOW",
                op=(">=" if inclusive else ">") if upward else ("<=" if inclusive else "<"),
                price=price,
                active_from=as_of,
                reason=reason,
                coordinate_id=coordinate_id,
            )
        )

    if candidate.stage == "COMPLETE":
        target_direction = -s
        amplitude = abs(p[-1] - p[0])
        targets = [
            (p[-1] - s * r * amplitude, "fibonacci_retracement") for r in (0.382, 0.5, 0.618)
        ]
        condition(
            "STAGE_EXIT", p[-1], s > 0, "越过已完成浪终点需要重新评估当前调整阶段", inclusive=False
        )
    elif candidate.template.name == "IMPULSE":
        l1 = abs(p[1] - p[0])
        if len(points) <= 4:
            origin = p[2]
            condition("INVALIDATION", p[0], s < 0, "浪2不可触及浪1起点")
            targets = [(origin + s * r * l1, "fibonacci_extension") for r in (1.0, 1.618, 2.618)]
            confirmation = p[1]
        else:
            origin = p[4]
            if candidate.stage == "W4_DEVELOPING":
                condition("INVALIDATION", p[1], s < 0, "普通推动浪4不可与浪1重叠")
            else:
                condition(
                    "STAGE_EXIT", p[4], s < 0, "跌向镜像的浪4边界结束当前浪5阶段", inclusive=False
                )
                l3 = abs(p[3] - p[2])
                if l3 < l1:
                    cap = p[4] + s * l3
                    condition("INVALIDATION", cap, s > 0, "超过此长度将使浪3最短", inclusive=False)
            targets = [(origin + s * r * l1, "fibonacci_extension") for r in (0.618, 1.0)]
            targets += [(origin + r * (p[3] - p[0]), "fibonacci_extension") for r in (0.382, 0.618)]
            duration = max(1, points[3].index - points[2].index)
            slope = (p[4] - p[2]) / (points[4].index - points[2].index)
            targets.append(
                (p[3] + slope * (points[4].index + duration - points[3].index), "channel")
            )
            confirmation = p[3]
        if s * (confirmation - current) > tick:
            condition("CONFIRMATION", confirmation, s > 0, "越过前一推动浪终点", inclusive=False)
    else:
        a = abs(p[1] - p[0])
        origin = p[2]
        if candidate.stage == "B_DEVELOPING":
            condition("INVALIDATION", p[0], s < 0, "锯齿B不可完全回撤A")
        else:
            condition("INVALIDATION", p[2], s < 0, "越过已确认B终点否定当前C阶段", inclusive=False)
        if s * (p[1] - current) > tick:
            condition("CONFIRMATION", p[1], s > 0, "C越过A终点", inclusive=False)
        targets = [(origin + s * r * a, "fibonacci_extension") for r in (1.0, 1.618)]

    feasible = sorted(
        [
            (value, method)
            for value, method in targets
            if value > 0
            and target_direction * (value - current) > tick
            and (cap is None or s * (value - cap) <= 0)
        ],
        key=lambda x: target_direction * (x[0] - current),
    )
    clusters: list[list[tuple[float, str]]] = []
    tolerance = max(0.5 * atr, 2 * tick)
    for item in feasible:
        if clusters and abs(item[0] - clusters[-1][-1][0]) <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    zones = []
    for group in clusters:
        values = [value for value, _ in group]
        methods = tuple(sorted({method for _, method in group}))
        low, high = min(values) - tolerance / 2, max(values) + tolerance / 2
        low = max(low, current + tick) if target_direction > 0 else max(tick, low)
        high = min(high, current - tick) if target_direction < 0 else high
        if cap is not None:
            if s > 0:
                high = min(high, cap)
            else:
                low = max(low, cap)
        if 0 < low <= high:
            zones.append(
                PriceZone(
                    low=low,
                    high=high,
                    label=f"目标区{len(zones) + 1}",
                    methods=methods,
                    independent_method_count=len(methods),
                    conditional=candidate.developing,
                )
            )
    duration = max(1, points[-1].index - points[-2].index)
    path = tuple(
        ProjectedPoint(
            label=f"T{i + 1}?",
            target_price=(zone.low + zone.high) / 2,
            min_bars=max(1, round(duration * 0.618)),
            reference_bars=duration,
            max_bars=max(duration, round(duration * 1.618)),
            timeframe=timeframe,
        )
        for i, zone in enumerate(zones)
    )
    return tuple(conditions), tuple(zones), path
