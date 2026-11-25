from collections.abc import Iterator
from pathlib import Path
from shlex import quote

from rich.console import Console
from rich.table import Table

from elliottwaver.schemas.brief import Brief, forecast_message
from elliottwaver.schemas.wave import AnalysisResult
from elliottwaver.visualization.brief_formatter import structure_evidence
from elliottwaver.visualization.brief_text import (
    DEGREE,
    METRIC_NOTE,
    PLAN_REASON_TEXT,
    PLAN_STATE_TEXT,
    adjustment_text,
    anchor_text,
    candidate_title,
    card_role,
    comparison_text,
    condition_price_text,
    diagnostic_text,
    direction_text,
    feed_text,
    forecast_row,
    guidance_sections,
    index_context_sections,
    intraday_track_text,
    market_time_text,
    mode_text,
    omission_text,
    outlook_heading,
    previous_change_text,
    price_text,
    status_text,
    summary_cards,
    target_range_text,
    timeframe_text,
)


def render_result(result: AnalysisResult) -> None:
    console = Console()
    prefix = "指数 · " if result.instrument.asset_type == "INDEX" else ""
    console.print(
        f"{prefix}{result.instrument.canonical_symbol} · {timeframe_text(result.timeframe)} · "
        f"行情截至 {result.market_asof} · {status_text(result.status)}",
        style="bold",
        markup=False,
    )
    console.print(
        f"{mode_text(result.mode)} · {feed_text(result.feed)} · {adjustment_text(result.adjustment)}",
        markup=False,
    )
    ambiguity = {"LOW": "较小", "MEDIUM": "中等", "HIGH": "较大"}[result.ambiguity]
    console.print(
        f"分析价格 {price_text(result.latest_price)} · 数浪分歧{ambiguity}"
        f" · 决策截止 {result.as_of.isoformat()}",
        markup=False,
    )
    for index, scenario in enumerate(result.scenarios, 1):
        zone = scenario.target_zones[0] if scenario.target_zones else None
        target = target_range_text(zone) if zone else "暂无目标"
        console.print(
            f"候选 {index} · {candidate_title(scenario)} · {DEGREE[scenario.degree]} · 第一目标 {target}",
            markup=False,
        )
        console.print(
            f"  子浪{status_text(scenario.subwave_status)} · 质量分 {scenario.score.quality_score:.1f}/100"
            f" · 证据覆盖 {scenario.score.coverage:.0%} · 候选权重 {scenario.relative_weight:.1%}",
            markup=False,
        )
        if scenario.structure is not None:
            for line in structure_evidence(scenario.structure):
                console.print(f"  {line}", markup=False)
    if result.scenarios:
        console.print(METRIC_NOTE, style="dim", markup=False)
    else:
        console.print("当前没有达到规则和证据要求的候选。")
    warnings = []
    for warning in result.warnings:
        code, _, message = warning.partition(":")
        warnings.append(diagnostic_text(code, message.strip()) if message else warning)
    for warning in dict.fromkeys(warnings):
        console.print(warning, style="yellow", markup=False)


def render_outlook(brief: Brief, *, console: Console | None = None) -> None:
    console = console or Console()
    if heading := outlook_heading(brief):
        console.print(heading, style="bold", markup=False)
    for title, lines in summary_cards(brief):
        console.print(title, style="bold", markup=False)
        for line in lines:
            console.print(f"  {line}", markup=False)
    if not brief.cards:
        console.print("当前没有达到规则和证据要求的候选。")


def _summary_lines(
    brief: Brief, *, is_index: bool, automatic: bool, config: str | Path | None
) -> Iterator[str]:
    intraday = brief.index_context.intraday if brief.index_context else None
    if brief.last_bar_end_at is not None:
        end = market_time_text(brief.last_bar_end_at, brief.symbol)
        if intraday:
            current = next((item for item in intraday.inputs if item.timeframe == "15m"), None)
            visible_by = (
                max(brief.as_of, brief.generated_at) if brief.mode == "LIVE" else brief.as_of
            )
            if current and current.last_bar_usable_at <= visible_by:
                end += f" · 可见 {market_time_text(current.last_bar_usable_at, brief.symbol)}"
        yield f"最近完整 K 线 {end}"
    if brief.status in {"STALE_DATA", "INSUFFICIENT_DATA"}:
        for issue in brief.issues:
            if issue.code == brief.status:
                yield diagnostic_text(issue.code, issue.message)
                break
    yield (
        f"主选 · {candidate_title(brief.cards[0])}"
        if brief.cards
        else "目前没有达到规则和证据要求的主选。"
    )
    if (previous := brief.previous) and previous.previous_digest is not None:
        yield f"与上次相比 · {previous_change_text(previous.change)}"
    if (cross := brief.cross_timeframe) and cross.state == "CONFLICT":
        yield f"主周期与{timeframe_text(cross.background_timeframe or '1w')}的方向有分歧。"

    if guidance := brief.index_guidance:
        digits = 3 if brief.symbol.endswith((".SH", ".SZ", ".BJ")) else 2
        for track in guidance.tracks:
            obs = track.observation
            horizon = "短线" if track.horizon == "SHORT" else "波段"
            state = PLAN_STATE_TEXT[obs.state] if obs.quality == "OK" else "数据待核"
            reason = PLAN_REASON_TEXT.get(obs.reason, obs.reason)
            yield f"{horizon} {track.holding_sessions} 个交易日 · {state} · {reason}"
            if track.candidate_wait_reason and track.candidate_wait_reason != obs.reason:
                reason = PLAN_REASON_TEXT.get(
                    track.candidate_wait_reason, track.candidate_wait_reason
                )
                yield f"  {reason}"
            if plan := track.plan:
                target = plan.targets[0]
                if obs.state in {"WAITING", "NEAR"}:
                    deadline = market_time_text(plan.activation_deadline, brief.symbol)
                    distance = track.trigger_distance_points
                    distance_text = f" · 距门槛 {distance:+.3f} 点" if distance is not None else ""
                    yield (
                        f"  触发条件 {condition_price_text(plan.trigger)}{distance_text} · 最迟 {deadline}"
                    )
                    if automatic and plan.periods[0].start_at > brief.as_of:
                        yield f"  {plan.periods[0].end_session} 起观察"
                    cap = f"{plan.max_entry_index_price:.{digits}f}"
                    about = "约 " if float(cap) != plan.max_entry_index_price else ""
                    yield f"  追价上限 {about}{cap} 点"
                yield f"  首目标 {target_range_text(target, digits=digits)}"
                yield "  失效条件 " + "、".join(map(condition_price_text, plan.invalidations))
                if plan.stage_exits and obs.state in {"WAITING", "NEAR", "TRIGGERED"}:
                    yield "  阶段退出 " + "、".join(map(condition_price_text, plan.stage_exits))
                if obs.due_session:
                    yield f"  持有到期 {obs.due_session}"
            for transition in track.transitions:
                if transition.from_state is not None:
                    change = PLAN_REASON_TEXT.get(transition.reason, transition.reason)
                    when = market_time_text(transition.observable_at, brief.symbol)
                    yield f"  本次变化 · {when} · {change}"
    elif intraday:
        yield f"60 分钟背景 · {direction_text(intraday.background_bias)}"
        for track in intraday.tracks:
            yield intraday_track_text(track)
        if all(track.status == "NO_DAILY_PLAN" for track in intraday.tracks):
            if brief.mode == "LIVE" and not automatic:
                command = f"elliottwaver brief {brief.symbol}"
                if config is not None:
                    command += f" --config {quote(str(config))}"
                yield f"先运行 {command} 查看同指数的收盘判断和预案状态。"
            elif brief.mode != "LIVE":
                yield "盘中回放需有当时已经发行的同指数日线回放预案。"
    elif brief.timeframe == "15m":
        background_bias = brief.cross_timeframe.background_bias if brief.cross_timeframe else None
        background = (
            direction_text(background_bias) if brief.background_state == "AVAILABLE" else "暂不可用"
        )
        yield f"60 分钟背景 · {background}"
        if is_index:
            yield "当前简报未启用日线预案的盘中跟踪。"

    if brief.cards and not (guidance and any(track.plan for track in guidance.tracks)):
        card = brief.cards[0]
        if not is_index:
            outlook = brief.outlook
            case = (
                next((item for item in outlook.cases if item.scenario_id == card.scenario_id), None)
                if outlook and outlook.status == "AVAILABLE"
                else None
            )
            if case and case.target is not None:
                timing = "推进后观察" if case.target_mode == "AFTER_ADVANCE" else "观察"
                yield f"{timing}目标区 · {target_range_text(case.target)}"
        conditions = [c for c in card.conditions if c.kind == "INVALIDATION"]
        if conditions:
            yield "主选失效条件 " + "、".join(map(condition_price_text, conditions))


def render_brief_summary(
    brief: Brief,
    *,
    is_index: bool,
    automatic: bool = False,
    config: str | Path | None = None,
    console: Console | None = None,
) -> None:
    console = console or Console()
    console.print(
        f"{brief.symbol} · {timeframe_text(brief.timeframe)} · 行情截至 {brief.market_asof} · "
        f"{status_text(brief.status)}",
        style="bold",
        markup=False,
    )
    price = price_text(brief.latest_price)
    price_label = f"指数 {price} 点" if is_index else f"分析价格 {price}"
    console.print(
        f"{mode_text(brief.mode)} · {feed_text(brief.feed)} · {price_label}", markup=False
    )
    if brief.retrospective:
        console.print("模型结果为回顾性重算。", style="yellow")
    for line in _summary_lines(brief, is_index=is_index, automatic=automatic, config=config):
        console.print(line, markup=False)
    if not automatic:
        console.print("详见报告，或用 --details 查看完整终端简报。", style="dim", markup=False)


def render_brief(brief: Brief, result: AnalysisResult | None = None) -> None:
    console = Console()
    intraday = brief.timeframe.endswith("m")
    console.print(
        f"{brief.symbol} · {'盘中简报' if intraday else '收盘摘要'} · "
        f"{timeframe_text(brief.timeframe)} · 行情截至 {brief.market_asof} · "
        f"{status_text(brief.status)}",
        style="bold",
        markup=False,
    )
    console.print(
        f"{mode_text(brief.mode)} · {feed_text(brief.feed)} · {adjustment_text(brief.adjustment)}",
        markup=False,
    )
    if intraday:
        console.print(
            f"市场时间 {market_time_text(brief.as_of, brief.symbol)} · "
            f"决策截止 {brief.as_of.isoformat()}",
            markup=False,
        )
        if brief.last_bar_end_at is not None:
            available = (
                market_time_text(brief.last_bar_available_at, brief.symbol)
                if brief.last_bar_available_at is not None
                else "未记录"
            )
            console.print(
                f"最后一根已完成 K 线 {market_time_text(brief.last_bar_end_at, brief.symbol)}"
                f" · 可用 {available}",
                markup=False,
            )
    console.print(f"分析价格 {price_text(brief.latest_price)}", markup=False)
    console.print()
    if sections := guidance_sections(brief):
        console.print("单指数预案", style="bold")
        for name, lines in sections:
            console.print(name, style="bold", markup=False)
            for line in lines:
                console.print(f"  {line}", markup=False)
    for name, lines in index_context_sections(brief):
        console.print(name, style="bold", markup=False)
        for line in lines:
            console.print(f"  {line}", markup=False)
    render_outlook(brief, console=console)
    console.print()
    for line in comparison_text(brief):
        console.print(line, markup=False)
    if brief.retrospective:
        console.print("模型结果为回顾性重算。", style="yellow")
    console.print(forecast_message(brief.forecast_status), style="dim", markup=False)
    if brief.forecasts:
        forecasts = Table("事件", "候选", "期限", "状态", "概率")
        for item in brief.forecasts:
            forecasts.add_row(*forecast_row(brief, item))
        console.print(forecasts)
    console.print()
    console.print("核对依据", style="bold")
    console.print(f"决策截止 {brief.as_of.isoformat()}", markup=False)
    console.print(
        f"分析范围 {brief.window.start} 至 {brief.window.end} · {brief.window.bar_count} 根 K 线",
        markup=False,
    )
    for anchor in brief.window.anchor_details:
        console.print(anchor_text(anchor), markup=False)
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    if brief.cards:
        console.print(METRIC_NOTE, style="dim", markup=False)
    for index, card in enumerate(brief.cards):
        role = card_role(index)
        if card.structure is not None:
            structure = card.structure
            evidence = (
                f" · 子浪已验证 {structure.verified_depth} 层，"
                f"检查深度为 {structure.requested_depth} 层"
            )
        else:
            evidence = f" · 子浪{status_text(card.subwave_status)}"
        console.print(
            f"{role} · {DEGREE[card.degree]} · 质量分 {card.quality_score:.1f}/100"
            f" · 证据覆盖 {card.coverage:.0%} · 候选权重 {card.relative_weight:.1%}{evidence}",
            markup=False,
        )
        if scenario := scenarios.get(card.scenario_id):
            counts = {
                status: sum(rule.status == status for rule in scenario.rules)
                for status in ("PASS", "FAIL", "PENDING")
            }
            console.print(
                f"  规则复核 · 通过 {counts['PASS']} · 违反 {counts['FAIL']}"
                f" · 证据不足 {counts['PENDING']}",
                markup=False,
            )
    if omitted := omission_text(brief):
        console.print(omitted, style="dim", markup=False)
    for issue in dict.fromkeys(
        diagnostic_text(issue.code, issue.message) for issue in brief.issues
    ):
        console.print(issue, style="yellow", markup=False)
    if brief.wave_relations:
        console.print(
            f"日周父子浪对应共 {len(brief.wave_relations)} 条，见报告的核对依据或 JSON。",
            style="dim",
            markup=False,
        )
