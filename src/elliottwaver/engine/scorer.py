"""Observable fit and evidence coverage, never a claimed success probability."""

import math
from collections.abc import Sequence

import numpy as np

from elliottwaver.config import ScoringProfile
from elliottwaver.engine.indicators import Indicators
from elliottwaver.engine.matcher import Candidate
from elliottwaver.schemas.market import Bar
from elliottwaver.schemas.wave import ScoreBreakdown, ScoreEvidence, WaveScenario


def fibonacci_fit(
    ratio: float, preferred: tuple[float, ...], sigma: float, secondary: tuple[float, ...] = ()
) -> float:
    if sigma <= 0 or not preferred:
        raise ValueError("positive sigma and preferred ratios required")
    return max(
        weight * math.exp(-((ratio - target) ** 2) / (2 * sigma**2))
        for values, weight in ((preferred, 1.0), (secondary, 0.7))
        for target in values
    )


def score(
    candidate: Candidate,
    bars: Sequence[Bar],
    indicators: Indicators,
    *,
    volume_comparable: bool,
    profile: ScoringProfile = "ew_score_v2",
    subwave_fit: float | None = None,
) -> ScoreBreakdown:
    if profile not in {"ew_score_v1", "ew_score_v2"}:
        raise ValueError(f"Unsupported scoring profile: {profile}")
    points = candidate.points
    q = [candidate.direction * p.price for p in points]
    evidence: list[ScoreEvidence] = []

    def add(feature, group, weight, value, fit, explanation, *, applicable=True):
        status = (
            "NOT_APPLICABLE" if not applicable else "UNAVAILABLE" if value is None else "AVAILABLE"
        )
        evidence.append(
            ScoreEvidence(
                feature=feature,
                group=group,
                weight=weight,
                value=value,
                fit=min(1, max(0, fit)),
                status=status,
                explanation=explanation,
            )
        )

    def segment(j: int):
        end = len(bars) - 1 if candidate.developing and j == len(points) - 1 else points[j].index
        return slice(points[j - 1].index + 1, end + 1)

    def mean_volume(j: int) -> float:
        data = bars[segment(j)]
        return float(np.mean([b.volume for b in data])) if data else 0

    ratios: list[
        tuple[str, float, tuple[float, ...], float, tuple[float, ...], float | None, str]
    ] = []
    impulse_like = candidate.template.name in {"IMPULSE", "DIAGONAL"}
    l1 = q[1] - q[0]
    if len(q) >= 3:
        ratios.append(
            (
                "retracement_2_or_B",
                (q[1] - q[2]) / l1,
                (0.5, 0.618),
                0.06,
                (0.382, 0.786),
                None,
                "",
            )
        )
    if len(q) >= 4:
        targets = (1.618, 2.618) if impulse_like else (1.0, 1.618)
        secondary = (1.382, 2.0, 4.236) if impulse_like else (0.618, 1.272)
        ratios.append(
            (
                "extension_3_or_C",
                (q[3] - q[2]) / l1,
                targets,
                0.15 if impulse_like else 0.10,
                secondary,
                None,
                "",
            )
        )
    if len(q) >= 5:
        ratios.append(
            (
                "retracement_4",
                (q[3] - q[4]) / (q[3] - q[2]),
                (0.382,),
                0.05,
                (0.236, 0.5),
                None,
                "",
            )
        )
    if len(q) >= 6 and impulse_like:
        # Keep the original fifth-wave formula selectable; v2 explicitly uses
        # the broader best-of-two policy rather than silently replacing v1.
        w5 = q[5] - q[4]
        w5_w1 = w5 / l1
        w5_w3 = w5 / (q[3] - q[2])
        if profile == "ew_score_v1":
            w1_targets, sigma, w1_secondary = (1.0, 0.618), 0.10, (1.618,)
            ratio, basis = w5_w1, "浪1"
            fit = fibonacci_fit(ratio, w1_targets, sigma, w1_secondary)
            explanation = f"实测比例 {ratio:.3f}（相对浪1）"
        else:
            w1_targets, sigma, w1_secondary = (0.618, 1.0, 1.618, 2.618), 0.15, (0.382, 4.236)
            w1_fit = fibonacci_fit(w5_w1, w1_targets, sigma, w1_secondary)
            w3_fit = fibonacci_fit(w5_w3, (0.618, 1.0, 1.618), sigma, (0.382, 2.618))
            if w1_fit >= w3_fit:
                ratio, fit, basis = w5_w1, w1_fit, "浪1"
            else:
                ratio, fit, basis = w5_w3, w3_fit, "浪3"
            explanation = f"实测比例 {ratio:.3f}（相对{basis}；浪1={w5_w1:.3f}，浪3={w5_w3:.3f}）"
        ratios.append(
            (
                "extension_5",
                ratio,
                w1_targets,
                sigma,
                w1_secondary,
                fit,
                explanation,
            )
        )
    for i, (name, ratio, targets, sigma, secondary, fit_override, explanation) in enumerate(ratios):
        tentative = candidate.developing and i == len(ratios) - 1
        fit = (
            fit_override
            if fit_override is not None
            else fibonacci_fit(ratio, targets, sigma, secondary)
        )
        text = explanation or f"实测比例 {ratio:.3f}"
        add(
            name,
            "fibonacci",
            40 / len(ratios),
            ratio,
            fit * (0.8 if tentative else 1),
            text + ("；进行中端点暂定，贡献折减20%" if tentative else ""),
        )

    impulse = impulse_like
    if impulse and len(points) >= 4:
        p1 = max(candidate.direction * indicators.dif[segment(1)])
        p3 = max(candidate.direction * indicators.dif[segment(3)])
        fit = float(p3 >= p1)
        add(
            "wave3_momentum",
            "momentum_volume",
            10,
            float(p3),
            fit,
            "截至分析时点浪3的有向DIF峰值与浪1比较",
        )
    if impulse and len(points) >= 6:
        p3 = max(candidate.direction * indicators.dif[segment(3)])
        p5 = max(candidate.direction * indicators.dif[segment(5)])
        applicable = q[5] > q[3]
        add(
            "wave5_divergence",
            "momentum_volume",
            10,
            float(p5),
            float(p5 < p3),
            "价格越过浪3后比较浪5与浪3的DIF峰值",
            applicable=applicable,
        )
    correction_ratios = []
    for j in (2, 4):
        if j < len(points):
            base = mean_volume(j - 1)
            if base > 0:
                correction_ratios.append(mean_volume(j) / base)
    vr = float(np.mean(correction_ratios)) if correction_ratios and volume_comparable else None
    add(
        "correction_volume",
        "momentum_volume",
        5,
        vr,
        float(vr <= 0.85) if vr is not None else 0,
        "调整段平均量与前一推动段比较；缺少可靠跨公司行为量口径时不评分",
    )

    if impulse and len(points) >= 5:
        r2, r4 = (q[1] - q[2]) / l1, (q[3] - q[4]) / (q[3] - q[2])
        t2, t4 = points[2].index - points[1].index, points[4].index - points[3].index
        alternate = (r2 > 0.5 and r4 < 0.382 and t4 > t2) or (r4 > 0.5 and r2 < 0.382 and t2 > t4)
        add("alternation", "guidelines", 10, abs(r2 - r4), float(alternate), "深浅回撤与耗时交替")
        x1, x2, x3, x4 = [p.index for p in points[1:5]]
        slope = (q[3] - q[1]) / (x3 - x1)
        expected = q[2] + slope * (x4 - x2)
        atr = max(float(indicators.atr[x4]), 1e-12)
        distance = abs(q[4] - expected) / atr
        add(
            "channel_4",
            "guidelines",
            10,
            distance,
            math.exp(-0.5 * distance**2),
            "P1-P3平行过P2的通道，用未参与拟合的P4验证",
        )
    if subwave_fit is None:
        add(
            "subwave_structure",
            "subwaves",
            15,
            None,
            0,
            "次级模板尚未认证；不以5/9笔数代替内部结构证明",
        )
    else:
        add(
            "subwave_structure",
            "subwaves",
            15,
            subwave_fit,
            subwave_fit,
            "子浪规则认证拟合；仅对通过硬规则的子结构计分",
        )
    expected = sum(e.weight for e in evidence if e.status != "NOT_APPLICABLE")
    available = sum(e.weight for e in evidence if e.status == "AVAILABLE")
    contribution = sum(e.weight * e.fit for e in evidence if e.status == "AVAILABLE")
    return ScoreBreakdown(
        quality_score=min(100, 100 * contribution / expected) if expected else 0,
        fit_score=min(100, 100 * contribution / available) if available else 0,
        coverage=min(1, available / expected) if expected else 0,
        evidence=tuple(evidence),
    )


def weight_scenarios(
    scenarios: Sequence[WaveScenario], temperature: float, top_k: int
) -> tuple[tuple[WaveScenario, ...], float]:
    if temperature <= 0 or top_k < 1:
        raise ValueError("positive temperature and top_k required")
    ordered = sorted(
        scenarios, key=lambda s: (-s.score.quality_score, -s.score.coverage, s.scenario_id)
    )
    if not ordered:
        return (), 0
    maximum = ordered[0].score.quality_score
    values = [math.exp((s.score.quality_score - maximum) / temperature) for s in ordered]
    total = sum(values)
    weighted = [
        s.model_copy(update={"relative_weight": v / total})
        for s, v in zip(ordered, values, strict=True)
    ]
    displayed = tuple(weighted[:top_k])
    return displayed, max(0, 1 - sum(s.relative_weight for s in displayed))
