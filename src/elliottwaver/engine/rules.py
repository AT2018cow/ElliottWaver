"""Hard constraints over both pivot endpoints and every intervening wick."""

from collections.abc import Sequence
from fractions import Fraction
from itertools import pairwise
from math import isclose

from elliottwaver.schemas.market import Bar
from elliottwaver.schemas.wave import PivotPoint, RuleResult


def _wave_return(start: float, end: float, direction: int) -> float:
    return direction * (end - start) / start


def wave5_length_cap(prices: Sequence[float], direction: int) -> float | None:
    """Return the price at which wave 5 has the same percentage length as wave 3."""
    p0, p1, p2, p3, p4 = prices[:5]
    first = _wave_return(p0, p1, direction)
    third = _wave_return(p2, p3, direction)
    if third >= first or isclose(third, first, rel_tol=1e-12):
        return None
    # The observed quote and the calculated cap must round only once to
    # binary float, so an exact decimal touch does not turn into a breach.
    return float(Fraction(str(p4)) * Fraction(str(p3)) / Fraction(str(p2)))


def _price_epsilon(prices: Sequence[float]) -> float:
    return max(1e-10, max(abs(price) for price in prices) * 1e-10)


def classify_variant(
    pattern: str, points: Sequence[PivotPoint], bars: Sequence[Bar], direction: int, tick: float
) -> str | None:
    """Classify the endpoint-level subtype that is decidable without a parent."""
    if len(points) < 4:
        return None
    q = [direction * point.price for point in points]
    if pattern == "FLAT" and len(q) >= 4:
        eps = _price_epsilon(q)
        a = abs(q[1] - q[0])
        if a <= eps:
            return None
        crossed_origin = q[2] < q[0] - eps
        if q[3] > q[1] + eps:
            return "EXPANDED_FLAT" if crossed_origin else "REGULAR_FLAT"
        if crossed_origin and q[3] < q[1] - eps:
            overshoot = q[0] - q[2]
            if overshoot >= max(tick, 0.10 * a) - eps:
                return "RUNNING_FLAT"
    if pattern == "TRIANGLE" and len(q) >= 6:
        eps = _price_epsilon(q)
        if q[3] < q[1] - eps and abs(q[4] - q[2]) <= tick + eps:
            return "BARRIER_TRIANGLE"
    if pattern == "DIAGONAL" and len(q) >= 6:
        first = abs(q[1] - q[0])
        third = abs(q[3] - q[2])
        fifth = abs(q[5] - q[4])
        if third < first and fifth < third:
            return "CONTRACTING_DIAGONAL"
        if third > first and fifth > third:
            return "EXPANDING_DIAGONAL"
    return None


def _rail_widths(
    points: Sequence[PivotPoint], q: Sequence[float], end_index: int
) -> tuple[float, float]:
    """Width between the 1–3 and 2–4 endpoint lines at the same bar indices."""
    x1, x2, x3, x4 = (point.index for point in points[1:5])
    upper = (q[3] - q[1]) / (x3 - x1)
    lower = (q[4] - q[2]) / (x4 - x2)
    width_at_second = q[1] + upper * (x2 - x1) - q[2]
    return width_at_second, width_at_second + (upper - lower) * (end_index - x2)


def check_pattern(
    pattern: str,
    points: Sequence[PivotPoint],
    bars: Sequence[Bar],
    direction: int,
    *,
    developing: bool,
    variant: str | None = None,
    tick_size: float = 0.01,
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
    prices = [point.price for point in points]
    q = [direction * price for price in prices]
    eps = _price_epsilon(q)
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
    if len(points) >= 3 and pattern == "WXY" and variant != "DOUBLE_THREE":
        low, _ = extremes(2)
        rule("X_ORIGIN", low > q[0] + eps, "双锯齿X不可触及W起点", low, q[0])
    elif len(points) >= 3 and pattern not in {"FLAT", "TRIANGLE", "WXY"}:
        low, _ = extremes(2)
        rule("W2_OR_B_ORIGIN", low > q[0] + eps, "浪2或锯齿B不可触及起点", low, q[0])
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
        if len(points) >= 3:
            a = abs(q[1] - q[0])
            b = abs(q[1] - q[2])
            deep = b >= 0.9 * a - eps
            rule(
                "B_DEEP",
                None if developing and len(points) == 3 and not deep else deep,
                "平台B至少回撤A的90%",
                b,
                0.9 * a,
            )
        if len(points) == 4:
            near = q[3] >= q[1] - 0.25 * a - eps
            if variant == "RUNNING_FLAT":
                rule(
                    "C_NEAR_A",
                    True,
                    "顺势平台允许C落在A终点之前",
                    q[3],
                    q[1] - 0.25 * a,
                )
            else:
                rule(
                    "C_NEAR_A",
                    None if developing and not near else near,
                    "平台C回到A终点附近或以上",
                    q[3],
                    q[1] - 0.25 * a,
                )
        return tuple(results)
    if pattern == "WXY":
        if variant == "DOUBLE_THREE":
            return tuple(results)
        if len(points) >= 3:
            a = abs(q[1] - q[0])
            x = abs(q[1] - q[2])
            deep = x >= 0.5 * a - eps
            rule(
                "X_RETRACE",
                None if developing and len(points) == 3 and not deep else deep,
                "双锯齿X至少回撤W的50%",
                x,
                0.5 * a,
            )
        if len(points) == 4:
            beyond = q[3] > q[1] + eps
            rule(
                "Y_BEYOND_W",
                None if developing and not beyond else beyond,
                "WXY的Y超过W终点",
                q[3],
                q[1],
            )
        return tuple(results)
    if pattern == "TRIANGLE":
        shape = "A-C 与 B-D 须在观察区间内形成未交叉的收敛或扩散边界"
        if len(points) < 5:
            rule("TRIANGLE_SHAPE", None, shape)
        else:
            end_index = len(bars) - 1 if developing else points[-1].index
            initial, final = _rail_widths(points, q, end_index)
            contracting = q[3] < q[1] - eps and q[4] > q[2] + eps
            expanding = q[3] > q[1] + eps and q[4] < q[2] - eps
            if variant == "BARRIER_TRIANGLE":
                contracting = q[3] < q[1] - eps and abs(q[4] - q[2]) <= tick_size + eps
                expanding = False
                shape = "阻挡三角形B-D近似水平且两条边界收敛"
            valid_rails = min(initial, final) > eps and (
                (contracting and final < initial - eps) or (expanding and final > initial + eps)
            )
            # A provisional D can still move down, while A and C are already fixed.
            if len(points) == 5 and developing:
                possible = q[3] > q[1] + eps or contracting
                valid = None if possible else False
            elif valid_rails and len(points) == 6:
                if contracting:
                    valid = q[5] < q[3] - eps
                else:
                    beyond = q[5] > q[3] + eps
                    valid = None if developing and not beyond else beyond
            else:
                valid = False
            rule("TRIANGLE_SHAPE", valid, shape, final, initial)
        end = q[5] if len(points) == 6 else None
        retraces = end is not None and end > q[0] + eps
        rule(
            "TRIANGLE_NET_RETRACEMENT",
            None if developing and not retraces else retraces,
            "三角形结束时E须越过A起点，形成对前一浪的净回撤",
            end,
            q[0],
        )
        return tuple(results)
    diagonal = pattern == "DIAGONAL"
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
        if diagonal:
            overlap = low <= q[1] + eps
            rule(
                "W4_OVERLAP",
                None if len(points) == 5 and developing and not overlap else overlap,
                "楔形浪4与浪1重叠",
                low,
                q[1],
            )
            rule("W4_NO_FULL_RETRACE", low > q[2] + eps, "楔形浪4不可完全回撤浪3", low, q[2])
        else:
            rule("W4_NO_OVERLAP", low > q[1] + eps, "普通推动浪4不进入浪1价格区", low, q[1])
    if diagonal:
        shape = "楔形的1-3与2-4轨道应收敛且不得在观察区间内相交"
        if len(points) >= 6:
            end_index = len(bars) - 1 if developing else points[-1].index
            initial, final = _rail_widths(points, q, end_index)
            rule(
                "DIAGONAL_SHAPE",
                q[4] > q[2] + eps and eps < final < initial - eps,
                shape,
                final,
                initial,
            )
        else:
            rule("DIAGONAL_SHAPE", None, shape)
    if len(points) >= 6:
        r1 = _wave_return(prices[0], prices[1], direction)
        r3 = _wave_return(prices[2], prices[3], direction)
        r5 = _wave_return(prices[4], prices[5], direction)
        price_cap = wave5_length_cap(prices, direction)
        cap = None if price_cap is None else direction * price_cap
        pending = developing and q[5] <= q[3] + eps
        truncated = (
            variant == "TRUNCATED_FIFTH" and len(points) >= 6 and q[4] + eps < q[5] <= q[3] + eps
        )
        rule(
            "W5_PROGRESS",
            True if truncated else (None if pending else q[5] > q[3] + eps),
            "截短第五浪允许浪5未越过浪3终点" if truncated else "完整非截短浪5须越过浪3终点",
            q[5],
            q[3],
        )
        rule(
            "W3_NOT_SHORTEST",
            cap is None or q[5] <= cap,
            "浪3不能短于浪1和浪5，浪长按各自起点的百分比（%）计算",
            100 * r3,
            100 * min(r1, r5),
        )
        if developing and cap is not None:
            _, high = extremes(5)
            rule(
                "W5_LENGTH_CAP",
                high <= cap,
                "浪3百分比短于浪1时，浪5百分比不得超过浪3",
                high,
                cap,
            )
    else:
        rule("W3_NOT_SHORTEST", None, "浪5未形成，不能按其未来长度否定当前计数")
    return tuple(results)
