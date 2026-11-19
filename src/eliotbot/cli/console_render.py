from rich.console import Console
from rich.table import Table

from eliotbot.schemas.brief import Brief, forecast_message
from eliotbot.schemas.wave import AnalysisResult
from eliotbot.visualization.brief_formatter import omission_text


def render_result(result: AnalysisResult) -> None:
    console = Console()
    console.print(
        f"[bold]EliotBot · {result.instrument.canonical_symbol}[/bold]  {result.timeframe}  {result.market_asof}  {result.feed}  {result.status}"
    )
    prefix = "指数 · " if result.instrument.asset_type == "INDEX" else ""
    console.print(
        f"{prefix}分析价格 {result.latest_price:.4f} · 结构歧义 {result.ambiguity} · 候选权重不是成功概率"
    )
    table = Table(
        "剧本", "浪级", "父级", "阶段", "子浪", "质量", "证据覆盖", "候选权重", "第一目标"
    )
    for i, scenario in enumerate(result.scenarios, 1):
        zone = scenario.target_zones[0] if scenario.target_zones else None
        parent = (
            "UNKNOWN"
            if scenario.parent_context == "UNKNOWN"
            else f"{scenario.parent_context}/{scenario.parent_degree}/{scenario.parent_wave_label}"
        )
        table.add_row(
            f"S{i}",
            scenario.degree,
            parent,
            scenario.stage,
            scenario.subwave_status,
            f"{scenario.score.quality_score:.1f}/100",
            f"{scenario.score.coverage:.0%}",
            f"{scenario.relative_weight:.1%}",
            f"{zone.low:.2f}–{zone.high:.2f}" if zone else "暂无合法目标",
        )
    console.print(table)
    for warning in result.warnings:
        console.print(warning, style="yellow", markup=False)
    if not result.scenarios:
        console.print("当前没有达到质量与证据门槛的候选。")


def render_brief(brief: Brief, result: AnalysisResult | None = None) -> None:
    console = Console()
    console.print(
        f"[bold]EliotBot Brief · {brief.symbol}[/bold]  {brief.timeframe}  "
        f"{brief.market_asof}  {brief.status}"
    )
    console.print(f"数据模式 {brief.mode} · 决策截止 {brief.as_of.isoformat()}", markup=False)
    console.print(f"周期选择：{brief.timeframe_reason or '—'}")
    console.print(
        f"窗口：{brief.window.start} → {brief.window.end} · "
        f"{brief.window.bar_count} 根 · 锚点 {len(brief.window.anchors)} 个"
    )
    for anchor in brief.window.anchor_details:
        confirm = (
            f" · {anchor.background_timeframe} 确认于 {anchor.confirmed_at}"
            if anchor.confirmed_at
            else ""
        )
        console.print(f"{anchor.date} · {anchor.origin}{confirm}", markup=False)
    if brief.retrospective:
        console.print("回顾性重算 · 使用显式指定的模型", style="yellow")
    table = Table("角色", "候选", "浪级", "阶段", "父级", "子浪", "质量", "覆盖", "权重")
    for card in brief.cards:
        parent = (
            "UNKNOWN"
            if card.parent_context == "UNKNOWN"
            else f"{card.parent_context}/{card.parent_degree}/{card.parent_wave_label}"
        )
        table.add_row(
            card.role,
            card.name,
            card.degree,
            card.stage,
            parent,
            card.subwave_status,
            f"{card.quality_score:.1f}/100",
            f"{card.coverage:.0%}",
            f"{card.relative_weight:.1%}",
        )
    console.print(table)
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    for card in brief.cards:
        scenario = scenarios.get(card.scenario_id)
        if scenario is None:
            continue
        counts = {
            status: sum(rule.status == status for rule in scenario.rules)
            for status in ("PASS", "FAIL", "PENDING")
        }
        console.print(
            f"规则复核 · {card.name} · 通过 {counts['PASS']} · "
            f"违反 {counts['FAIL']} · 证据不足 {counts['PENDING']}",
            markup=False,
        )
    if not brief.cards:
        console.print("当前没有达到质量与证据门槛的候选。")
    console.print(omission_text(brief), style="dim", markup=False)
    for issue in brief.issues:
        console.print(f"{issue.code}: {issue.message}", style="yellow", markup=False)
    if brief.forecasts:
        forecasts = Table("事件", "候选", "期限", "状态", "概率")
        for item in brief.forecasts:
            forecasts.add_row(
                item.event_id,
                item.scenario_id or "标的",
                f"{item.horizon_sessions} {item.horizon_unit}",
                item.status,
                ", ".join(f"{key} {value:.1%}" for key, value in item.probabilities.items())
                if item.probabilities
                else item.reason or "—",
            )
        console.print(forecasts)
    console.print(forecast_message(brief.forecast_status), style="dim", markup=False)
