from rich.console import Console
from rich.table import Table

from elliottwaver.schemas.brief import Brief, forecast_message
from elliottwaver.schemas.wave import AnalysisResult
from elliottwaver.visualization.brief_formatter import structure_evidence
from elliottwaver.visualization.brief_text import (
    DEGREE,
    METRIC_NOTE,
    adjustment_text,
    anchor_text,
    candidate_title,
    card_role,
    comparison_text,
    diagnostic_text,
    feed_text,
    forecast_row,
    market_time_text,
    mode_text,
    omission_text,
    outlook_heading,
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
