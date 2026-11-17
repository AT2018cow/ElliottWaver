from rich.console import Console
from rich.table import Table

from eliotbot.schemas.wave import AnalysisResult


def render_result(result: AnalysisResult) -> None:
    console = Console()
    console.print(
        f"[bold]EliotBot · {result.instrument.canonical_symbol}[/bold]  {result.timeframe}  {result.market_asof}  {result.feed}  {result.status}"
    )
    console.print(
        f"分析价格 {result.latest_price:.4f} · 结构歧义 {result.ambiguity} · 候选权重不是成功概率"
    )
    table = Table("剧本", "阶段", "质量", "证据覆盖", "候选权重", "第一目标")
    for i, scenario in enumerate(result.scenarios, 1):
        zone = scenario.target_zones[0] if scenario.target_zones else None
        table.add_row(
            f"S{i}",
            scenario.stage,
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
