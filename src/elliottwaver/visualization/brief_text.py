"""Shared user-facing wording for terminal and exported briefs."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from elliottwaver.schemas.brief import Brief, BriefAnchor, BriefCard, BriefForecast, BriefPeriod
from elliottwaver.schemas.index_context import IntradayTrack
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


def target_range_text(zone: PriceZone, *, digits: int = 2) -> str:
    exact = f"{price_text(zone.low)}–{price_text(zone.high)}"
    low, high = f"{zone.low:.{digits}f}", f"{zone.high:.{digits}f}"
    if low == high or float(low) == 0:
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


def condition_price_text(condition: LevelCondition) -> str:
    side = {"HIGH": "最高价", "LOW": "最低价", "CLOSE": "收盘价"}[condition.side]
    op = {"<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于"}[condition.op]
    return f"{side}{op} {price_text(condition.price)}"


def condition_text(condition: LevelCondition, role: str | None = None) -> str:
    label = CONDITION.get(role or condition.kind, "触发条件")
    return f"{label} · {condition_price_text(condition)}（{condition.reason}）"


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
        "tencent-cn-index": "腾讯指数行情",
        "tushare-cn-index": "Tushare 指数行情",
        "yahoo-index": "Yahoo 指数行情",
        "massive-index": "Massive 指数行情",
        "sip": "Alpaca 合并行情（SIP）",
        "iex": "Alpaca IEX 行情",
        "synthetic": "合成演示行情",
    }.get(value, value or "未配置")


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
        lines.append(f"相较上次 · {previous_change_text(previous.change)}")
    return tuple(lines)


def previous_change_text(value: str) -> str:
    return {
        "FIRST_RECORD": "首次记录",
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
    }.get(value, status_text(value))


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


PLAN_STATE_TEXT = {
    "WAITING": "等待",
    "NEAR": "接近触发",
    "TRIGGERED": "指数条件已触发",
    "TARGET_REACHED": "指数目标已达",
    "INVALIDATED": "已失效",
    "STAGE_EXIT": "阶段退出",
    "EXPIRED": "已到期",
    "CANCELLED": "已取消",
    "AMBIGUOUS": "同根先后待核",
}
PLAN_REASON_TEXT = {
    "WAIT_FOR_CLOSE": "等待下个有效收盘",
    "NO_QUALIFIED_CARD": "等待达到规则和证据要求的主选",
    "DATA_PENDING": "等待完整且可比的日线行情",
    "CALENDAR_UNAVAILABLE": "等待可核验的完整交易日历",
    "DIRECTION_NOT_LONG": "等待向上推进情景",
    "NO_ADVANCE_GATE": "等待明确的上行推进门槛",
    "STOP_ALREADY_HIT": "当前结构的退出条件已触及",
    "NO_VALID_INVALIDATION": "等待确认位下方的结构失效条件",
    "NO_UPSIDE_TARGET": "等待有效的上方目标",
    "UNCONFIRMED_PIVOT": "等待用于预案的拐点确认",
    "NO_ENTRY_RANGE": "等待确认位与目标之间具备足够空间",
    "TARGET_ALREADY_REACHED": "首目标已进入或越过",
    "BACKGROUND_CONFLICT": "等待日周方向冲突解除",
    "BACKGROUND_MISSING": "等待完整周线背景",
    "WINDOW_CONFLICT": "等待换窗方向分歧解除",
    "NEAR_TIE": "等待主备质量分拉开或证据发生变化",
    "WAIT_FOR_STABLE_RECOUNT": "等待下一根完整日线仍支持新主选",
    "WAIT_FOR_INDEPENDENT_STRUCTURE": "等待出现新的结构证据",
    "CLOSE_TRIGGER": "指数收盘达到冻结条件",
    "HARD_INVALIDATION": "触及冻结失效条件",
    "STAGE_EXIT": "触及冻结阶段退出条件",
    "INDEX_TARGET_REACHED": "激活后首次达到冻结目标",
    "INDEX_CHASE_LIMIT": "指数收盘超过追价上限",
    "ACTIVATION_DEADLINE": "到最迟触发时刻仍未激活",
    "HOLDING_DEADLINE": "已到冻结持有期限",
    "SAME_BAR_TARGET_AND_EXIT": "同根出现目标和退出",
    "SAME_BAR_TARGET_AND_STAGE_EXIT": "同根触及目标和阶段退出",
    "SAME_BAR_TRIGGER_AND_EXIT": "同根出现触发和退出",
    "MISSING_SESSION_BAR": "交易会话行情尚未齐备",
    "CALENDAR_NOT_COVERED": "输入行情未覆盖冻结交易会话",
    "DATA_REVISED": "行情已修订，历史观测保留",
    "NOT_COMPARABLE": "与原发行行情口径未能对齐",
    "PRICE_COORDINATE_CHANGED": "行情的指数点位口径与发行时不一致",
    "POLICY_CHANGED": "预案政策变化",
    "STRUCTURE_REPLACED": "新主选持续后替换旧预案",
    "PLAN_ISSUED": "已发行",
}
PLAN_PRIORITY = {"INVALIDATED": 0, "STAGE_EXIT": 0, "TRIGGERED": 1, "NEAR": 2}


def guidance_sections(brief: Brief) -> tuple[tuple[str, tuple[str, ...]], ...]:
    guidance = brief.index_guidance
    if guidance is None:
        return ()
    results = []

    def priority(track):
        return min(
            (PLAN_PRIORITY.get(item.to_state, 3) for item in track.transitions),
            default=3,
        )

    for track in sorted(guidance.tracks, key=priority):
        plan, obs = track.plan, track.observation
        term = "短线" if track.horizon == "SHORT" else "波段"
        state = PLAN_STATE_TEXT[obs.state] if obs.quality == "OK" else "数据待核"
        title = f"{term} · {track.holding_sessions} 会话 · {state}"
        reasons = PLAN_REASON_TEXT.get(obs.reason, obs.reason)
        lines = [
            f"{reasons} · 指数 {price_text(guidance.last_price)} 点 · 行情年龄 {guidance.last_price_age_seconds / 60:g} 分钟"
        ]
        if track.candidate_wait_reason and track.candidate_wait_reason != obs.reason:
            lines.append(
                "下一步 "
                + PLAN_REASON_TEXT.get(track.candidate_wait_reason, track.candidate_wait_reason)
            )
        if plan:
            lines.append(
                f"收盘 {plan.trigger.op} {price_text(plan.trigger.price)} · "
                f"距触发 {track.trigger_distance_points:+.3f} 点 / "
                f"{track.trigger_distance_atr:+.2f} ATR · "
                f"指数追价上限 {price_text(plan.max_entry_index_price)}"
            )
            lines.append(
                f"最迟触发 {market_time_text(plan.activation_deadline, brief.symbol)} · "
                + (f"持有到期 {obs.due_session}" if obs.due_session else "触发会话计为持有第 1 日")
            )
            lines.append(
                "失效 "
                + "、".join(
                    f"{ {'LOW': '最低', 'HIGH': '最高', 'CLOSE': '收盘'}[c.side] } {c.op} {price_text(c.price)}"
                    for c in plan.invalidations
                )
            )
            if plan.stage_exits:
                lines.append(
                    "阶段退出 "
                    + "、".join(
                        f"{ {'LOW': '最低', 'HIGH': '最高', 'CLOSE': '收盘'}[c.side] } {c.op} {price_text(c.price)}"
                        for c in plan.stage_exits
                    )
                )
            zones = plan.targets
        else:
            zones = tuple(item.zone for item in guidance.target_distances)
        if zones:
            distances = track.current_targets if plan else guidance.target_distances
            distance = next((item for item in distances if item.zone == zones[0]), None)
            text = "目标 " + "、".join(
                f"{price_text(z.low)}–{price_text(z.high)}" for z in zones[:3]
            )
            if distance is not None:
                loc = {"AHEAD": "未达", "IN_ZONE": "已进入", "PASSED": "已越过"}[distance.location]
                text += f" · {loc} · {distance.points:+.3f} 点"
                if distance.atr is not None:
                    text += f" / {distance.atr:+.2f} ATR"
            lines.append(text)
        evidence = plan.confirmation if plan else guidance.confirmation
        if evidence:
            lines.append(
                f"极值至确认 {evidence.extreme_to_confirmation_seconds / 3600:g} 小时 · "
                f"确认指数 {price_text(evidence.confirmation_price)} 点"
            )
        if obs.next_index_price:
            quote = obs.next_index_price
            delay = f"确认后 {(quote.confirmation_to_price_seconds / 3600):g} 小时"
            move = f"{quote.moved_points:+.3f} 点"
            if quote.moved_atr is not None:
                move += f" / {quote.moved_atr:+.2f} ATR"
            lines.append(f"下一可观察指数价 {price_text(quote.price)} 点 · {delay}已走 {move}")
        if track.transitions:
            ordered = sorted(
                track.transitions, key=lambda item: PLAN_PRIORITY.get(item.to_state, 3)
            )
            changes = "、".join(
                f"{item.session} {PLAN_REASON_TEXT.get(item.reason, item.reason)}"
                for item in ordered
            )
            lines.append(f"本次变化 {changes}")
        if guidance.sizing:
            sizing = guidance.sizing
            lines.append(
                f"手工输入 {sizing.inputs.instrument} · 估算最多买 {sizing.buy_limit} 份 · "
                f"输入可卖 {sizing.sell_limit} 份"
            )
        elif guidance.tool_status == "SIZING_INPUT_OUTSIDE_WINDOW":
            lines.append("手工工具输入未生效或已过期")
        else:
            lines.append("执行工具未配置")
        results.append((title, tuple(lines)))
    return tuple(results)


CONTEXT_STATUS_TEXT = {
    "AVAILABLE": "可用",
    "NOT_CONFIGURED": "未配置",
    "INPUT_MISSING": "等待数据",
    "PERCENTILE_HISTORY_INSUFFICIENT": "分位样本不足",
    "INSUFFICIENT_HISTORY": "历史不足",
    "INCOMPLETE_HISTORY": "存在行情缺口",
    "REFERENCE_SNAPSHOT_REQUIRED": "等待固定参照快照",
    "REFERENCE_INPUT_UNAVAILABLE": "参照行情不可用",
    "STALE_REFERENCE": "参照行情晚到或过期",
    "SAME_INDEX": "参照与主指数相同",
    "NOT_COMPARABLE": "标的、币种或价格口径不可比",
    "INCOMPLETE_COMMON_SESSIONS": "共同交易日有缺口",
    "CALENDAR_MISMATCH": "交易日历不一致",
    "COVERAGE_UNAVAILABLE": "日历覆盖未提供",
    "INSUFFICIENT_COVERAGE": "覆盖不足",
    "MEMBERSHIP_UNAVAILABLE": "成分名单或行情不足",
    "NO_DAILY_PLAN": "暂无已发行日线预案",
    "PARENT_INACTIVE": "日线预案不可激活",
    "PARENT_RECEIPT_UNAVAILABLE": "日线发行回执待核",
    "PARENT_NOT_VISIBLE": "日线预案当时尚未发行",
    "POLICY_NOT_FROZEN": "旧预案缺少冻结的盘中规则",
    "RECEIPT_NOT_VISIBLE": "盘中行情尚未收到",
    "BACKGROUND_60M_NOT_READY": "等待可用的 60 分钟背景",
    "PRICE_BASIS_UNVERIFIED": "待核对日线与盘中指数点位",
    "MISSING_15M_SLOT": "15 分钟行情有缺口",
    "STALE_15M": "15 分钟行情过期",
    "STALE_CROSSING": "触及时的证据晚到",
    "PARENT_INVALIDATED": "日线预案盘中已失效",
    "PARENT_EXPIRED": "已超过日线预案激活期限",
    "TRIGGERED": "指数点位已触发",
    "WAITING": "等待盘中确认",
    "AMBIGUOUS": "同根先后待核",
    "CHASE_LIMIT": "超过指数追价上限",
    "VOLUME_NOT_COMPARABLE": "量能口径不可比",
}


def intraday_track_text(track: IntradayTrack) -> str:
    methods = {"TOUCH": "触价", "CLOSE": "收盘确认", "RETEST": "突破回踩"}
    horizon = "短线" if track.horizon == "SHORT" else "波段"
    threshold = f" · 门槛 {price_text(track.threshold)}" if track.threshold is not None else ""
    mode = f"15 分钟{methods[track.selected_method]} · " if track.selected_method else ""
    return f"{horizon} · {mode}{CONTEXT_STATUS_TEXT.get(track.status, track.status)}{threshold}"


def index_context_sections(brief: Brief) -> tuple[tuple[str, tuple[str, ...]], ...]:
    context = brief.index_context
    if context is None:
        return ()

    def label(status):
        return CONTEXT_STATUS_TEXT.get(status, status)

    sections = []
    trend = context.trend
    reference = context.reference
    breadth = context.breadth
    if trend:
        lines = []
        if trend.return_pct is None:
            lines.append(f"主指数背景 · {label(trend.status)}")
        else:
            lines.append(
                f"{trend.start} 至 {trend.end} · {trend.sessions} 会话收益 {trend.return_pct:+.2f}%"
            )
            if trend.volatility_percentile is not None:
                lines.append(
                    f"日波动历史分位 {trend.volatility_percentile:.1f}% · {trend.prior_windows} 个参考窗口"
                )
            if trend.latest_overnight_gap_pct is not None:
                lines.append(f"最近已观测隔夜跳空 {trend.latest_overnight_gap_pct:+.2f}%")
        if reference and reference.symbol:
            lines.append(
                f"较 {reference.symbol} 的收益差 {reference.spread_pp:+.2f} 个百分点"
                if reference.spread_pp is not None
                else f"参照 {reference.symbol} · {label(reference.status)}"
            )
        if breadth and breadth.members:
            lines.append(
                f"成分上涨 {breadth.rising}/{breadth.comparable} · 可比成分 {breadth.comparable}/{breadth.members} · {label(breadth.status)}"
            )
        sections.append(("市场背景", tuple(lines)))
    if context.intraday:
        intraday = context.intraday
        lines = [
            f"60 分钟背景 · {direction_text(intraday.background_bias)}",
            *(intraday_track_text(track) for track in intraday.tracks),
        ]
        if intraday.volume.value is not None:
            lines.append(
                f"同源同槽相对量 {intraday.volume.value:.2f} 倍 · {intraday.volume.sample_days} 日"
            )
        sections.append(("日线预案盘中跟踪", tuple(lines)))
    if context.events:
        lines = []
        for window in context.events:
            horizon = "短线" if window.horizon == "SHORT" else "波段"
            lines.append(
                f"{horizon} {window.start} 至 {window.end} · 调样 {label(window.rebalance_status)} · 业绩 {label(window.earnings_status)}"
            )
            if window.closed_weekdays:
                lines.append("工作日休市 " + "、".join(map(str, window.closed_weekdays)))
            if window.earnings_event_weight is not None:
                lines.append(f"已知业绩披露权重 {window.earnings_event_weight:.1%}")
            for row in window.events:
                kind = "指数调样" if row.kind == "REBALANCE" else "业绩披露"
                lines.append(
                    f"{row.symbol} {kind} · {market_time_text(row.window_timestamp(brief.as_of), brief.symbol)}"
                )
        if next_open := context.events[0].next_open_at:
            lines.append(f"下次开市 {market_time_text(next_open, brief.symbol)}")
        sections.append(("持有期已知事件", tuple(lines)))
    return tuple(sections)
