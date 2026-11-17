"""Hard constraints over both pivot endpoints and every intervening wick."""

from collections.abc import Sequence
from itertools import pairwise

from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import PivotPoint, RuleResult


def check_pattern(
    pattern: str,
    points: Sequence[PivotPoint],
    bars: Sequence[Bar],
    direction: int,
    *,
    developing: bool,
) -> tuple[RuleResult, ...]:
    if pattern not in {
        "IMPULSE",
        "ZIGZAG_ABC",
        "TRIANGLE",
        "FLAT",
        "DIAGONAL",
        "WXY",
    } or direction not in {-1, 1}:
        raise ValueError("unsupported rule profile")
    q = [direction * p.price for p in points]
    eps = max(1e-10, max(abs(x) for x in q) * 1e-10)
    results: list[RuleResult] = []

    def rule(name: str, valid: bool | None, reason: str, observed=None, boundary=None):
        results.append(
            RuleResult(
                rule_id=name,
                status="PENDING" if valid is None else "PASS" if valid else "FAIL",
                reason=reason,
                observed=observed,
                boundary=boundary,
            )
        )

    def extremes(segment: int) -> tuple[float, float]:
        end = len(bars) - 1 if developing and segment == len(points) - 1 else points[segment].index
        # The departure bar has unknown intrabar order; do not claim its other wick is later.
        subset = bars[points[segment - 1].index + 1 : end + 1]
        values = [direction * v for b in subset for v in (b.low, b.high)]
        return (min(values), max(values)) if values else (q[segment], q[segment])

    ordered = all(a.index < b.index for a, b in pairwise(points))
    alternating = all(a.kind != b.kind for a, b in pairwise(points))
    rule("ORDER", ordered and alternating, "端点时间递增且高低交替")
    if not ordered or not alternating:
        return tuple(results)
    for j in range(1, len(points)):
        going_up = j % 2 == 1
        valid = q[j] > q[j - 1] + eps if going_up else q[j] < q[j - 1] - eps
        rule(f"DIRECTION_{j}", valid, "浪段方向与模板一致")
        low, high = extremes(j)
        extrema_valid = (
            high <= q[j] + eps and low >= q[j - 1] - eps
            if going_up
            else low >= q[j] - eps and high <= q[j - 1] + eps
        )
        rule(f"SEGMENT_EXTREMA_{j}", extrema_valid, "合并未隐藏更极端的内部影线")
    if len(points) >= 3 and pattern != "FLAT":
        low, _ = extremes(2)
        rule("W2_OR_B_ORIGIN", low > q[0] + eps, "浪2或锯齿B不可触及起点", low, q[0])
    elif pattern == "FLAT" and len(points) >= 3:
        rule("W2_OR_B_ORIGIN", None, "平台B允许超过起点，由B_DEEP检查")
    else:
        rule("W2_OR_B_ORIGIN", None, "尚无第二反向段")
    if pattern == "ZIGZAG_ABC":
        if len(points) == 4:
            rule(
                "C_PROGRESS",
                None if developing and q[3] <= q[1] + eps else q[3] > q[1] + eps,
                "完整非截短C须越过A终点",
                q[3],
                q[1],
            )
        return tuple(results)
    if pattern == "FLAT":
        if len(points) == 4:
            a = abs(q[1] - q[0])
            b = abs(q[1] - q[2])
            rule("B_DEEP", b >= 0.9 * a - eps, "平台B至少回撤A的90%", b, 0.9 * a)
            rule(
                "C_NEAR_A",
                q[3] >= q[1] - 0.25 * a - eps,
                "平台C回到A终点附近或以上",
                q[3],
                q[1] - 0.25 * a,
            )
        return tuple(results)
    if pattern == "WXY":
        if len(points) == 4:
            a = abs(q[1] - q[0])
            x = abs(q[1] - q[2])
            rule(
                "X_RETRACE",
                0.5 * a - eps <= x <= a + eps,
                "WXY的X回撤W的50%-100%",
                x,
                a,
            )
            rule("Y_BEYOND_W", q[3] > q[1] + eps, "WXY的Y超过W终点", q[3], q[1])
        return tuple(results)
    if pattern == "TRIANGLE":
        if len(points) == 6:
            legs = [abs(q[index] - q[index - 1]) for index in range(1, 6)]
            contracting = (
                legs[2] < legs[0] - eps and legs[4] < legs[2] - eps and legs[3] < legs[1] - eps
            )
            expanding = (
                legs[2] > legs[0] + eps and legs[4] > legs[2] + eps and legs[3] > legs[1] + eps
            )
            rule(
                "TRIANGLE_SHAPE",
                contracting or expanding,
                "三角形应收敛或扩散",
                legs[2] / legs[0] if legs[0] else None,
                1.0,
            )
        else:
            rule("TRIANGLE_SHAPE", None, "三角形尚未完成")
        return tuple(results)
    if pattern == "DIAGONAL":
        if len(points) >= 4:
            pending = developing and len(points) == 4 and q[3] <= q[1] + eps
            rule(
                "W3_PROGRESS",
                None if pending else q[3] > q[1] + eps,
                "完成浪3须越过浪1终点",
                q[3],
                q[1],
            )
        if len(points) >= 5:
            rule(
                "W4_OVERLAP",
                q[4] <= q[1] + eps,
                "楔形浪4与浪1重叠",
                q[4],
                q[1],
            )
        if len(points) >= 6:
            rule(
                "W5_PROGRESS",
                None if q[5] <= q[3] + eps else True,
                "楔形允许截断第五浪",
                q[5],
                q[3],
            )
        return tuple(results)
    if len(points) >= 4:
        pending = developing and len(points) == 4 and q[3] <= q[1] + eps
        rule(
            "W3_PROGRESS",
            None if pending else q[3] > q[1] + eps,
            "完成浪3须越过浪1终点",
            q[3],
            q[1],
        )
    if len(points) >= 5:
        low, _ = extremes(4)
        rule("W4_NO_OVERLAP", low > q[1] + eps, "普通推动浪4不进入浪1价格区", low, q[1])
    if len(points) >= 6:
        l1, l3, l5 = q[1] - q[0], q[3] - q[2], q[5] - q[4]
        pending = developing and q[5] <= q[3] + eps
        rule(
            "W5_PROGRESS",
            None if pending else q[5] > q[3] + eps,
            "完整非截短浪5须越过浪3终点",
            q[5],
            q[3],
        )
        rule("W3_NOT_SHORTEST", l3 + eps >= min(l1, l5), "浪3不能短于浪1和浪5", l3, min(l1, l5))
        if developing and l3 < l1 - eps:
            _, high = extremes(5)
            rule(
                "W5_LENGTH_CAP",
                high <= q[4] + l3 + eps,
                "浪3短于浪1时浪5不得超过浪3长度",
                high,
                q[4] + l3,
            )
    else:
        rule("W3_NOT_SHORTEST", None, "浪5未形成，不能按其未来长度否定当前计数")
    return tuple(results)
