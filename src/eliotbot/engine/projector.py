"""Conditional targets subject to the same hard boundaries as the count."""

from collections.abc import Sequence
from datetime import datetime

from eliotbot.engine.matcher import Candidate
from eliotbot.engine.rules import wave5_length_cap
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
    target_floor: float | None = None
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
        # A completed pattern can retrace deeper than 61.8%; without the 78.6%/100%
        # levels a market that has already passed 61.8% is reported as having no target.
        targets = [
            (p[-1] - s * r * amplitude, "fibonacci_retracement")
            for r in (0.382, 0.5, 0.618, 0.786, 1.0)
        ]
        condition(
            "STAGE_EXIT", p[-1], s > 0, "越过已完成浪终点需要重新评估当前调整阶段", inclusive=False
        )
    elif candidate.template.name in {"IMPULSE", "DIAGONAL"}:
        l1 = abs(p[1] - p[0])
        if len(points) <= 4:
            origin = p[2]
            condition("INVALIDATION", p[0], s < 0, "浪2不可触及浪1起点")
            targets = [(origin + s * r * l1, "fibonacci_extension") for r in (1.0, 1.618, 2.618)]
            confirmation = p[1]
        else:
            origin = p[4]
            if candidate.stage == "W4_DEVELOPING":
                if candidate.template.name == "DIAGONAL":
                    condition("INVALIDATION", p[2], s < 0, "楔形浪4不可完全回撤浪3")
                else:
                    condition("INVALIDATION", p[1], s < 0, "普通推动浪4不可与浪1重叠")
            else:
                if candidate.template.name == "DIAGONAL":
                    condition(
                        "INVALIDATION", p[4], s < 0, "越过已确认浪4端点否定当前浪5", inclusive=False
                    )
                else:
                    condition(
                        "STAGE_EXIT",
                        p[4],
                        s < 0,
                        "跌向镜像的浪4边界结束当前浪5阶段",
                        inclusive=False,
                    )
                cap = wave5_length_cap(p, s)
                if cap is not None:
                    condition(
                        "INVALIDATION", cap, s > 0, "越界后按百分比计算浪3最短", inclusive=False
                    )
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
    elif candidate.template.name == "TRIANGLE":
        if len(points) >= 6:
            origin = p[-1]
            width = max(abs(p[1] - p[0]), abs(p[3] - p[2]), abs(p[5] - p[4]))
            condition("STAGE_EXIT", p[-1], s > 0, "越过三角形E点结束当前阶段", inclusive=False)
            if s * (p[3] - current) > tick:
                condition("CONFIRMATION", p[3], s > 0, "越过三角形C点确认突破", inclusive=False)
            targets = [(origin + s * r * width, "fibonacci_extension") for r in (1.0, 1.618)]
        else:
            condition("STAGE_EXIT", p[-1], s > 0, "三角形尚未完成", inclusive=False)
            targets = []
    elif candidate.template.name == "FLAT":
        a = abs(p[1] - p[0])
        if candidate.stage == "B_DEVELOPING":
            depth = p[1] - s * 0.9 * a
            if s * (p[2] - depth) > tick:
                condition("CONFIRMATION", depth, s < 0, "达到平台B的90%最低回撤")
            condition("STAGE_EXIT", p[1], s > 0, "越过A端点时重新评估B浪", inclusive=False)
        else:
            condition("INVALIDATION", p[2], s < 0, "越过已确认B端点否定平台C", inclusive=False)
            boundary = p[1] - s * 0.25 * a
            if s * (boundary - current) > tick:
                condition("CONFIRMATION", boundary, s > 0, "平台C进入A端点附近")
        target_floor = p[1] - s * 0.25 * a
        targets = [(p[2] + s * r * a, "fibonacci_extension") for r in (1.0, 1.618)]
    elif candidate.template.name == "WXY":
        w = abs(p[1] - p[0])
        if candidate.stage == "X_DEVELOPING":
            condition("INVALIDATION", p[0], s < 0, "双锯齿X不可触及W起点")
            depth = p[1] - s * 0.5 * w
            if s * (p[2] - depth) > tick:
                condition("CONFIRMATION", depth, s < 0, "达到双锯齿X的50%最低回撤")
        else:
            condition("INVALIDATION", p[2], s < 0, "越过已确认X端点否定双锯齿Y", inclusive=False)
        if s * (p[1] - current) > tick:
            condition("CONFIRMATION", p[1], s > 0, "双锯齿Y越过W终点", inclusive=False)
        target_floor = p[1] + s * tick
        targets = [(p[2] + s * r * w, "fibonacci_extension") for r in (1.0, 1.618)]
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
            and (target_floor is None or s * (value - target_floor) >= 0)
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
        if target_floor is not None:
            if s > 0:
                low = max(low, target_floor)
            else:
                high = min(high, target_floor)
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
