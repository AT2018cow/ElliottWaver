"""Shared user-facing wording for terminal and exported briefs."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from elliottwaver.schemas.brief import Brief, BriefAnchor, BriefCard, BriefForecast, BriefPeriod
from elliottwaver.schemas.outlook import OutlookCase
from elliottwaver.schemas.wave import LevelCondition, PriceZone, WaveScenario

STATUS = {
    "OK": "分析正常",
    "UNCLEAR": "判断尚不明确",
    "INSUFFICIENT_DATA": "行情不足",
    "STALE_DATA": "行情已过期",
    "SEARCH_LIMITED": "搜索达到上限，候选可能不完整",
    "VERIFIED": "已验证",
    "PROVISIONAL": "待核实",
    "PENDING": "待确认",
    "UNVERIFIED": "尚未验证",
    "UNAVAILABLE": "暂无证据",
    "UNRESOLVED": "尚未找到匹配结构",
    "REJECTED": "未通过规则",
    "UNDETERMINED": "无法判断",
    "INCOMPATIBLE": "结构不匹配",
    "UNKNOWN": "未找到匹配",
    "APPROVED": "已验证可发布",
    "UNCALIBRATED": "尚未校准",
    "INSUFFICIENT_EVIDENCE": "验证证据不足",
    "OUT_OF_SCOPE": "超出模型适用范围",
    "UNDEFINED_EVENT": "缺少可观察条件",
    "UNSUPPORTED_BIN": "该概率区间的验证样本不足",
    "MODEL_INVALID": "模型不可用",
    "TRIGGERED": "已触发",
    "OBSERVED_AFTER_GAP": "缺口后观测到触及，无法判断是否为首次触发",
    "AMBIGUOUS_SAME_BAR": "同一根 K 线内先后无法判断",
}
DEGREE = {
    "MINOR": "小浪级",
    "INTERMEDIATE": "中浪级",
    "PRIMARY": "主浪级",
    "CYCLE": "循环浪级",
    "SUPERCYCLE": "超级循环浪级",
    "UNKNOWN": "浪级待定",
}
PATTERN = {
    "IMPULSE": "推动浪",
    "ZIGZAG_ABC": "锯齿形 ABC",
    "TRIANGLE": "三角形",
    "FLAT": "平台形",
    "DIAGONAL": "楔形",
    "WXY": "双锯齿 WXY",
    "DOUBLE_ZIGZAG": "双锯齿 WXY",
    "DOUBLE_THREE": "双重三浪 WXY",
    "TRUNCATED_FIFTH": "第 5 浪截短的推动浪",
    "REGULAR_FLAT": "普通平台形",
    "EXPANDED_FLAT": "扩展平台形",
    "RUNNING_FLAT": "顺势平台形",
    "CONTRACTING_TRIANGLE": "收敛三角形",
    "EXPANDING_TRIANGLE": "扩散三角形",
    "BARRIER_TRIANGLE": "阻挡三角形",
    "CONTRACTING_DIAGONAL": "收敛楔形",
    "EXPANDING_DIAGONAL": "扩散楔形",
    "LEADING_DIAGONAL": "引导楔形",
    "ENDING_DIAGONAL": "终结楔形",
}
CONDITION = {
    "ADVANCE": "推进信号",
    "CURRENT_EVIDENCE": "当前浪的验证条件",
    "CONFIRMATION": "确认条件",
    "INVALIDATION": "失效条件",
    "STAGE_EXIT": "需重新数浪",
}
STOP_REASONS = {
    "NO_ADVANCE_GATE": "暂无下一浪的推进门槛",
    "NO_VALID_TARGET": "当前暂无可用目标",
    "NO_STOP": "暂无可用的失效条件",
    "STOP_ALREADY_HIT": "发行时已触发停止条件，需重新数浪",
}
METRIC_NOTE = (
    "质量分衡量形态吻合程度，覆盖率表示可用评分证据的比例。候选权重只衡量本次候选之间的相对支持度。"
)


def price_text(value: float) -> str:
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def target_range_text(zone: PriceZone) -> str:
    exact = f"{price_text(zone.low)}–{price_text(zone.high)}"
    low, high = f"{zone.low:.2f}", f"{zone.high:.2f}"
    if low == high or low == "0.00":
        return exact
    if float(low) == zone.low and float(high) == zone.high:
        return exact
    return f"约 {low}–{high}"


def timeframe_text(value: str) -> str:
    labels = {"1d": "日线", "1w": "周线", "1mo": "月线", "1h": "60 分钟线"}
    return labels.get(value, f"{value[:-1]} 分钟线" if value.endswith("m") else value)


def status_text(value: str) -> str:
    return STATUS.get(value, "状态待查")


def direction_text(value: str | None) -> str:
    return {"UP": "偏上行", "DOWN": "偏下行"}.get(value, "方向待定")


def stage_text(stage: str) -> str:
    if stage == "COMPLETE":
        return "结构已完成"
    if stage.endswith("_DEVELOPING"):
        wave = stage.removesuffix("_DEVELOPING")
        if wave.startswith("W") and wave[1:].isdigit():
            return f"第 {wave[1:]} 浪进行中"
        return f"{wave} 浪进行中"
    return "阶段待定"


def candidate_title(candidate: BriefCard | WaveScenario) -> str:
    name = PATTERN.get(candidate.variant or "", PATTERN[candidate.pattern_type])
    direction = "上涨" if candidate.pattern_direction == "UP" else "下跌"
    return f"{direction}{name} · {stage_text(candidate.stage)}"


def card_role(index: int) -> str:
    return "主选" if index == 0 else f"备选 {index}"


def condition_text(condition: LevelCondition, role: str | None = None) -> str:
    side = {"HIGH": "最高价", "LOW": "最低价", "CLOSE": "收盘价"}[condition.side]
    op = {"<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于"}[condition.op]
    label = CONDITION.get(role or condition.kind, "触发条件")
    return f"{label} · {side}{op} {price_text(condition.price)}（{condition.reason}）"


def mode_text(value: str) -> str:
    return {
        "LIVE": "实时运行",
        "FROZEN_DATA_REPLAY": "使用已保存行情回放",
        "POINT_IN_TIME_REPLAY": "按时点回放",
    }.get(value, "数据模式待查")


def feed_text(value: str) -> str:
    return {
        "sina": "新浪个股行情",
        "sina-index": "新浪指数行情",
        "sip": "Alpaca 合并行情（SIP）",
        "iex": "Alpaca IEX 行情",
        "synthetic": "合成演示行情",
    }.get(value, value)


def adjustment_text(value: str) -> str:
    return {
        "basis_date_qfq": "以前复权价格分析",
        "provider_all": "按供应商口径复权",
        "provider_raw": "未复权",
        "unadjusted": "未复权",
        "none": "未复权",
        "synthetic_no_actions": "演示数据无复权调整",
    }.get(value, value)


def _market_zone(symbol: str) -> ZoneInfo:
    if symbol.endswith((".SH", ".SZ", ".BJ")):
        return ZoneInfo("Asia/Shanghai")
    return ZoneInfo("America/New_York")


def market_time_text(value: datetime, symbol: str) -> str:
    return value.astimezone(_market_zone(symbol)).strftime("%Y-%m-%d %H:%M %Z")


def anchor_text(anchor: BriefAnchor) -> str:
    background = (
        timeframe_text(anchor.background_timeframe) if anchor.background_timeframe else "背景行情"
    )
    origin = {
        "EXPLICIT": "手动指定的起点",
        "FALLBACK": "从可用历史中选取起点",
        "MACRO_HIGH": f"从{background}高点选取",
        "MACRO_LOW": f"从{background}低点选取",
    }[anchor.origin]
    when = anchor.at.isoformat() if anchor.at else str(anchor.date)
    confirmed = f" · {anchor.confirmed_at.isoformat()} 确认" if anchor.confirmed_at else ""
    return f"{when} · {origin}{confirmed}"


def horizon_text(horizon: int, unit: str) -> str:
    if unit == "BARS":
        return f"{horizon} 根主周期 K 线"
    return f"{horizon} 个{'完整交易周' if unit == 'WEEKS' else '交易日'}"


def outlook_heading(brief: Brief) -> str | None:
    outlook = brief.outlook
    if outlook is None:
        return None
    if outlook.status == "AVAILABLE" and outlook.periods:
        if outlook.horizon_unit == "BARS":
            first = market_time_text(outlook.periods[0].start_at, brief.symbol)
            last = market_time_text(outlook.periods[-1].end_at, brief.symbol)
            return (
                f"短期观察 · 后续 {horizon_text(outlook.horizon, outlook.horizon_unit)}"
                f" · {first} 至 {last}"
            )
        return (
            f"短期观察 · 后续 {horizon_text(outlook.horizon, outlook.horizon_unit)}"
            f" · 到期 {outlook.periods[-1].end_session}"
        )
    code = (outlook.reason or "").partition(":")[0]
    reason = {
        "CALENDAR_UNAVAILABLE": "交易日历不可用",
        "INSUFFICIENT_CALENDAR": "缺少未来完整交易日安排",
        "OUTLOOK_UNAVAILABLE": "暂无符合条件的候选",
    }.get(code)
    if reason is None:
        reason = status_text(brief.status) if brief.status not in {"OK", "UNCLEAR"} else "证据不足"
    return f"短期观察暂不可用 · {reason}"


def _case_lines(case: OutlookCase) -> tuple[str, ...]:
    reasons = {event.reason for event in case.events if event.state == "UNDEFINED" and event.reason}
    if "STOP_ALREADY_HIT" in reasons:
        return (STOP_REASONS["STOP_ALREADY_HIT"],)
    target_note = next(
        (
            STOP_REASONS[event.reason]
            for event in case.events
            if event.event_id.startswith("OUTLOOK_TARGET_FIRST")
            and event.state == "UNDEFINED"
            and event.reason in {"NO_ADVANCE_GATE", "NO_STOP"}
        ),
        None,
    )
    if case.target is None:
        target = "暂无目标（未找到满足条件的区间）"
    else:
        timing = "目标等待推进后观察" if case.target_mode == "AFTER_ADVANCE" else "目标立即观察"
        if target_note:
            timing = f"目标区仅供参考（{target_note}）"
        direction = (
            f"（{direction_text(case.target_direction)}）"
            if not case.active_wave or case.current_leg_direction != case.target_direction
            else ""
        )
        target = f"{timing} · {target_range_text(case.target)}{direction}"
    title = f"当前浪{direction_text(case.current_leg_direction)} · " if case.active_wave else ""
    return (
        f"{title}{target}",
        *(
            condition_text(item.condition, item.role)
            for role in ("ADVANCE", "INVALIDATION", "STAGE_EXIT", "CURRENT_EVIDENCE")
            for item in case.conditions
            if item.role == role
        ),
    )


def summary_cards(brief: Brief) -> tuple[tuple[str, tuple[str, ...]], ...]:
    outlook = brief.outlook
    cases = {
        case.scenario_id: case
        for case in (
            outlook.cases if outlook and outlook.status == "AVAILABLE" and outlook.periods else ()
        )
    }
    cards = []
    for index, card in enumerate(brief.cards):
        title = f"{card_role(index)} · {candidate_title(card)}"
        if case := cases.get(card.scenario_id):
            lines = _case_lines(case)
        elif outlook is None or outlook.status != "AVAILABLE" or not outlook.periods:
            lines = (
                *(condition_text(condition) for condition in card.conditions),
                *(
                    f"{zone.label} · {price_text(zone.low)}–{price_text(zone.high)}"
                    for zone in card.target_zones[:1]
                ),
            )
        else:
            lines = ()
        cards.append((title, lines))
    return tuple(cards)


def comparison_text(brief: Brief) -> tuple[str, ...]:
    lines = []
    if cross := brief.cross_timeframe:
        labels = {
            "ALIGNED": "日线与周线的下一段方向一致",
            "CONFLICT": "日线与周线的下一段方向有分歧",
            "UNDETERMINED": "日线或周线的下一段方向未明确",
            "NOT_AVAILABLE": "缺少可对照的周线结果",
        }
        if cross.background_bias is not None:
            suffix = (
                f" · 主周期{direction_text(cross.primary_bias)}，"
                f"{timeframe_text(cross.background_timeframe)}"
                f"{direction_text(cross.background_bias)}"
                if cross.state in {"ALIGNED", "CONFLICT"}
                else ""
            )
        else:
            suffix = (
                f" · 日线{direction_text(cross.primary_bias)}，"
                f"周线{direction_text(cross.weekly_bias)}"
                if cross.state in {"ALIGNED", "CONFLICT"}
                else ""
            )
        if cross.state == "NOT_AVAILABLE" and brief.timeframe.endswith("m"):
            lines.append(cross.reason)
        elif cross.state == "ALIGNED" and cross.background_bias is not None:
            lines.append(
                f"主周期与{timeframe_text(cross.background_timeframe)}下一段方向一致{suffix}"
            )
        elif cross.state == "CONFLICT" and cross.background_bias is not None:
            lines.append(
                f"主周期与{timeframe_text(cross.background_timeframe)}下一段方向有分歧{suffix}"
            )
        else:
            lines.append(f"{labels[cross.state]}{suffix}")
    if (window := brief.window_comparison) and window.state != "NOT_APPLICABLE":
        labels = {
            "SAME_COUNT": "首选数浪一致",
            "CHANGED_COUNT_SAME_BIAS": "首选数浪有变化，方向一致",
            "CHANGED_BIAS": "方向发生变化",
            "INCONCLUSIVE": "证据不足，暂无结论",
        }
        lines.append(f"换起点复核 · {labels[window.state]}")
    if (previous := brief.previous) and previous.change != "FIRST_RECORD":
        labels = {
            "SAME_COUNT": "维持原数浪",
            "ALTERNATE_PROMOTED": "原备选成为主选",
            "STAGE_ADVANCED": "浪段已推进",
            "COUNT_CHANGED": "数浪已改变",
            "TIP_MOVED": "进行中端点移动",
            "HARD_INVALIDATED": "原数浪已失效",
            "EVIDENCE_CHANGED": "支持证据有变化",
            "CONFIG_CHANGED": "配置或规则版本有变化",
            "DATA_REVISED": "历史行情有修订",
            "NOT_COMPARABLE": "两次结果不可直接比较",
        }
        lines.append(f"相较上次 · {labels.get(previous.change, status_text(previous.change))}")
    return tuple(lines)


def omission_text(period: Brief | BriefPeriod) -> str:
    lines = []
    if period.omitted_weight:
        lines.append(f"超出候选列表上限的权重为 {period.omitted_weight:.1%}。")
    if period.omitted_top_k_count and period.omitted_top_k_weight is not None:
        lines.append(
            f"列表中另有 {period.omitted_top_k_count} 个候选未列入简报"
            f"（权重 {period.omitted_top_k_weight:.1%}）。"
        )
    return "".join(lines)


def forecast_row(brief: Brief, item: BriefForecast) -> tuple[str, str, str, str, str]:
    events = {
        "CLOSE_BAND": "收盘方向与波动区间",
        "CONFIRM_FIRST": "确认条件先触发",
        "ADVANCE_FIRST": "推进条件先触发",
        "TARGET_FIRST": "目标先到达",
        "OUTLOOK_TARGET_FIRST": "短期目标先到达",
    }
    event = events.get(item.event_id.rsplit("_", 2)[0], "价格事件")
    candidate = next(
        (
            card_role(index)
            for index, card in enumerate(brief.cards)
            if card.scenario_id == item.scenario_id
        ),
        "其他候选" if item.scenario_id else "标的",
    )
    classes = {
        "UP": "上涨",
        "DOWN": "下跌",
        "RANGE": "区间内",
        "YES": "发生",
        "NO": "未发生",
    }
    probability = "，".join(
        f"{classes.get(name, '其他')} {value:.1%}"
        for name, value in (item.probabilities or {}).items()
    )
    return (
        event,
        candidate,
        horizon_text(item.horizon_sessions, item.horizon_unit),
        status_text(item.status),
        probability or "—",
    )


def diagnostic_text(code: str, message: str) -> str:
    labels = {
        "PARENT_CONTEXT_PROVISIONAL": "部分候选与更大浪形的对应关系仍待核实。",
        "MIXED_DEGREE_SCENARIOS": "候选来自不同浪级，宜在同一浪级内比较。",
        "EXCLUDED_STRUCTURE": "有候选因评分或可用证据未达要求而被省略。",
        "ANALYSIS_WINDOW_TRUNCATED": "起点搜索只使用最近一段行情。",
        "DATA_START_CLIPPED": "可用行情起点晚于请求日期。",
        "ALLOWED_GAPS": "行情有缺口，本次按请求继续分析。",
        "IDENTITY_UNVERIFIED": "标的身份来自离线输入，尚未经在线核实。",
        "MODEL_INVALID": "概率模型加载失败，见摘要 JSON 的诊断记录。",
        "ANCHOR_FALLBACK": "未确认背景拐点，已从可用历史中另选起点。",
        "SEARCH_LIMITED": "搜索达到上限，部分候选可能未被检查。",
    }
    if code == "ENGINE_WARNING" and "PIT" in message:
        suffix = "跨公司行动前后的成交量可比性尚待核实。" if "量比" in message else ""
        return f"历史回放使用本次取得的复权数据，当时可见的版本可能不同。{suffix}"
    if code in labels:
        return labels[code]
    if message and any("\u4e00" <= char <= "\u9fff" for char in message):
        return message
    return "另有诊断详情，见报告目录中的 JSON。"
