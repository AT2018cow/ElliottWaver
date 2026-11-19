"""Review blind daily wave endpoints against verified OHLC and explicit rules."""

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import date
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import DataError, InputError
from eliotbot.schemas.market import AnalysisRequest, MarketDataBundle
from eliotbot.service import prepare_bundle
from eliotbot.util import atomic_write, canonical_json


def _rule(name, valid, explanation, observed=None, boundary=None):
    return {
        "rule": name,
        "status": "PASS" if valid else "FAIL",
        "evidence": explanation,
        "observed": observed,
        "boundary": boundary,
    }


def _pattern_checks(prices: list[float], positions: list[int], lows: list[float]) -> dict:
    """Return endpoint-level verdicts. Internal counts require finer pivots."""
    q = prices
    eps = max(1e-10, max(map(abs, q)) * 1e-10)
    if len(q) == 4:
        a, b = q[1] - q[0], q[1] - q[2]
        stay = _rule("B_OR_X_ORIGIN", lows[1] > q[0] + eps, "第二段影线未触及起点", lows[1], q[0])
        progress = _rule("C_OR_Y_PROGRESS", q[3] > q[1] + eps, "第三段越过第一段终点", q[3], q[1])
        return {
            "ZIGZAG_ABC": [stay, progress],
            "FLAT": [
                _rule("B_RETRACE", b + eps >= 0.9 * a, "B 回撤不少于 A 的 90%", b, 0.9 * a),
                _rule(
                    "C_NEAR_A",
                    q[3] + eps >= q[1] - 0.25 * a,
                    "C 回到 A 终点附近或以上",
                    q[3],
                    q[1] - 0.25 * a,
                ),
            ],
            "WXY": [
                stay,
                _rule("X_RETRACE", b + eps >= 0.5 * a, "X 回撤不少于 W 的 50%", b, 0.5 * a),
                progress,
            ],
        }
    if len(q) != 6:
        return {}
    length_1, length_3, length_5 = (q[i] - q[i - 1] for i in (1, 3, 5))
    common = [
        _rule("W2_ORIGIN", lows[1] > q[0] + eps, "第 2 浪未触及起点", lows[1], q[0]),
        _rule("W3_PROGRESS", q[3] > q[1] + eps, "第 3 浪越过第 1 浪终点", q[3], q[1]),
        _rule("W5_PROGRESS", q[5] > q[3] + eps, "第 5 浪越过第 3 浪终点", q[5], q[3]),
        _rule(
            "W3_NOT_SHORTEST",
            length_3 + eps >= min(length_1, length_5),
            "第 3 浪不是第 1、3、5 浪中最短的",
            length_3,
            min(length_1, length_5),
        ),
    ]
    a, b, c, d, end = positions[1:]
    upper_slope = (q[3] - q[1]) / (c - a)
    lower_slope = (q[4] - q[2]) / (d - b)
    width_start = q[1] + upper_slope * (b - a) - q[2]
    width_end = width_start + (upper_slope - lower_slope) * (end - b)
    contracting = q[3] < q[1] - eps and q[4] > q[2] + eps and q[5] < q[3] - eps
    expanding = q[3] > q[1] + eps and q[4] < q[2] - eps and q[5] > q[3] + eps
    triangle = min(width_start, width_end) > eps and (
        contracting and width_end < width_start - eps or expanding and width_end > width_start + eps
    )
    return {
        "IMPULSE": [
            *common,
            _rule(
                "W4_NO_OVERLAP",
                lows[3] > q[1] + eps,
                "第 4 浪影线未进入第 1 浪价格区",
                lows[3],
                q[1],
            ),
        ],
        "DIAGONAL": [
            *common,
            _rule("W4_OVERLAP", lows[3] <= q[1] + eps, "楔形第 4 浪与第 1 浪重叠", lows[3], q[1]),
            _rule(
                "W4_NO_FULL_RETRACE",
                lows[3] > q[2] + eps,
                "楔形第 4 浪未完全回撤第 3 浪",
                lows[3],
                q[2],
            ),
            _rule(
                "DIAGONAL_RAILS",
                q[4] > q[2] + eps and eps < width_end < width_start - eps,
                "楔形双轨收敛且未相交",
                width_end,
                width_start,
            ),
        ],
        "TRIANGLE": [
            _rule(
                "TRIANGLE_RAILS",
                triangle,
                "A–C 和 B–D 双轨有效且 E 延续该边界",
                width_end,
                width_start,
            )
        ],
    }


def review_case(case: dict, bundle: MarketDataBundle | None) -> dict:
    endpoints = case["endpoints"]
    row = {key: case[key] for key in ("case_id", "symbol", "decision_date")}
    row.update({"status": "PENDING", "scope": "ENDPOINT_GEOMETRY", "reason": "", "templates": {}})
    if not endpoints or bundle is None or any(not item.get("date") for item in endpoints):
        row["reason"] = "缺少可核对端点、日期或完整行情"
        return row
    days = [date.fromisoformat(item["date"]) for item in endpoints]
    kinds = [item["kind"] for item in endpoints]
    if not all(a < b for a, b in pairwise(days)) or not all(
        a != b and {a, b} == {"LOW", "HIGH"} for a, b in pairwise(kinds)
    ):
        row.update(status="FAIL", reason="端点必须按时间递增，且高低交替")
        return row
    indexed = {bar.session_date: (index, bar) for index, bar in enumerate(bundle.bars)}
    decision = date.fromisoformat(case["decision_date"])
    if not (
        days[-1] <= decision == bundle.bars[-1].session_date
        and len(days) >= 2
        and all(day in indexed for day in days)
    ) or any(day not in indexed for day in bundle.sessions if days[0] <= day <= days[-1]):
        row["reason"] = "浪段行情或截止时点缺少可核对的会话"
        return row
    positions = [indexed[day][0] for day in days]
    p = [float(item["price"]) for item in endpoints]
    if not all(math.isfinite(x) and x > 0 for x in p):
        row.update(status="FAIL", reason="端点价格应为有限正数")
        return row
    for item, day, value in zip(endpoints, days, p, strict=True):
        bar = indexed[day][1]
        price = bar.low if item["kind"] == "LOW" else bar.high
        if not math.isclose(price, value, rel_tol=1e-9, abs_tol=0.000051):
            row.update(status="FAIL", reason=f"{day} 端点价格与该方向影线不符")
            return row
    direction = 1 if kinds[0] == "LOW" else -1
    q = [direction * value for value in p]
    lows, basics = [], []
    for i, (a, b) in enumerate(pairwise(positions), 1):
        segment = bundle.bars[a + 1 : b + 1]
        wicks = [direction * wick for bar in segment for wick in (bar.low, bar.high)]
        low, high = min(wicks), max(wicks)
        lower, upper = sorted(q[i - 1 : i + 1])
        lows.append(low)
        basics.extend(
            (
                _rule(
                    f"DIRECTION_{i}",
                    q[i] > q[i - 1] if i % 2 else q[i] < q[i - 1],
                    "浪段按起点方向交替",
                ),
                _rule(
                    f"WICKS_{i}",
                    low >= lower - 0.000051 and high <= upper + 0.000051,
                    "浪段内部影线落在端点价格区间",
                ),
            )
        )
    row["direction"] = "UP" if direction == 1 else "DOWN"
    row["endpoints"] = endpoints
    row["basis"] = "SIGNED_PRICE" if direction == -1 else "PRICE"
    row["common_rules"] = basics
    if any(rule["status"] == "FAIL" for rule in basics):
        row.update(status="FAIL", reason="有浪段的方向或内部影线违反端点约束")
        return row
    for name, checks in _pattern_checks(q, positions, lows).items():
        row["templates"][name] = {
            "status": "PASS" if all(r["status"] == "PASS" for r in checks) else "FAIL",
            "rules": checks,
        }
    matches = any(value["status"] == "PASS" for value in row["templates"].values())
    if matches:
        row.update(status="PASS", reason="至少一类端点模板通过硬规则，内部子浪保留待核")
    else:
        row["reason"] = "当前已确认端点未支持完整模板，保留进行中或其他划分"
    return row


def run(cases_path: Path, cache_root: Path, output: Path) -> dict:
    content = cases_path.read_bytes()
    cases = json.loads(content)["cases"]
    if not cases or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Review requires non-empty cases with unique identities")
    store = SnapshotStore(cache_root)

    @lru_cache(maxsize=20)
    def source(snapshot_id):
        return store.load(snapshot_id)

    @lru_cache(maxsize=20)
    def prepared(snapshot_id, symbol, decision):
        bundle = source(snapshot_id)
        bar = next(item for item in bundle.bars if item.session_date == decision)
        return prepare_bundle(
            AnalysisRequest(
                symbol=symbol, as_of=bar.usable_at, end=decision, mode="FROZEN_DATA_REPLAY"
            ),
            bundle,
        )

    reviews = []
    for case in cases:
        bundle = None
        if case["timeframe"] == "1d" and case.get("source_snapshot_id") and case["endpoints"]:
            try:
                bundle = prepared(
                    case["source_snapshot_id"],
                    case["symbol"],
                    date.fromisoformat(case["decision_date"]),
                )
            except (DataError, InputError, StopIteration, ValueError):
                pass
        reviews.append(review_case(case, bundle))
    result = {
        "schema_version": "1.0",
        "method": "INDEPENDENT_ENDPOINT_RULE_REVIEW",
        "cases_sha256": hashlib.sha256(content).hexdigest(),
        "counts": dict(Counter(item["status"] for item in reviews)),
        "reviews": reviews,
    }
    atomic_write(output, canonical_json(result) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=Path(".cache/eliotbot"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.cases, args.cache_root, args.output)
    print(canonical_json(result["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
