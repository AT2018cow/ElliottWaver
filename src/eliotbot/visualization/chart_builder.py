"""Render a canonical result; never recompute wave labels or scores."""

import html
import io
import os
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from eliotbot.engine.indicators import calculate
from eliotbot.errors import ExportError
from eliotbot.schemas.market import MarketDataBundle
from eliotbot.schemas.wave import AnalysisResult


def bar_label(bar, result):
    if result.timeframe.endswith("m"):
        return bar.start_at.astimezone(ZoneInfo(result.instrument.timezone)).strftime("%m-%d %H:%M")
    return str(bar.session_date)


def build_plotly(result: AnalysisResult, bundle: MarketDataBundle):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as exc:
        raise ExportError(
            "Plotly is unavailable; install project dependencies or use --renderer svg"
        ) from exc
    bars = bundle.bars
    indicators = calculate(bars)
    x = list(range(len(bars)))
    future = len(bars) + max(
        (p.reference_bars for s in result.scenarios for p in s.projected_path), default=25
    )
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, row_heights=[0.65, 0.15, 0.20], vertical_spacing=0.035
    )
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=[b.open for b in bars],
            high=[b.high for b in bars],
            low=[b.low for b in bars],
            close=[b.close for b in bars],
            name="OHLC",
            text=[
                f"{bar_label(b, result)} · {b.start_at.isoformat()} → {b.end_at.isoformat()}"
                for b in bars
            ],
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(x=x, y=[b.volume for b in bars], name="Volume", marker_color="#8295b1"), row=2, col=1
    )
    for values, name in ((indicators.dif, "DIF"), (indicators.dea, "DEA")):
        fig.add_trace(go.Scatter(x=x, y=values, name=name, mode="lines"), row=3, col=1)
    fig.add_trace(go.Bar(x=x, y=indicators.histogram, name="MACD histogram"), row=3, col=1)
    base_count = len(fig.data)
    trace_groups, shape_groups = [], []
    for i, scenario in enumerate(result.scenarios):
        indices = []
        waves = scenario.completed_waves
        fig.add_trace(
            go.Scatter(
                x=[w.pivot.index for w in waves],
                y=[w.pivot.price for w in waves],
                text=[f"({w.label})" for w in waves],
                mode="lines+markers+text",
                textposition="top center",
                line={"color": "#3d83d9", "width": 3},
                customdata=[[str(w.pivot.extreme_at), str(w.pivot.confirmed_at)] for w in waves],
                hovertemplate="%{text}<br>extreme=%{customdata[0]}<br>confirmed=%{customdata[1]}<extra></extra>",
                visible=i == 0,
                name=scenario.stage,
            ),
            row=1,
            col=1,
        )
        indices.append(len(fig.data) - 1)
        tip = scenario.developing_tip
        fig.add_trace(
            go.Scatter(
                x=[waves[-1].pivot.index, tip.index],
                y=[waves[-1].pivot.price, tip.price],
                mode="lines+markers",
                line={"dash": "dash", "color": "#bf883c"},
                name="Developing (unconfirmed)",
                visible=i == 0,
            ),
            row=1,
            col=1,
        )
        indices.append(len(fig.data) - 1)
        if scenario.projected_path:
            point = scenario.projected_path[0]
            fig.add_trace(
                go.Scatter(
                    x=[len(bars) - 1, len(bars) - 1 + point.reference_bars],
                    y=[result.latest_price, point.target_price],
                    mode="lines+markers",
                    line={"dash": "dot", "color": "#4ab58c"},
                    name="Conditional reference path",
                    visible=i == 0,
                ),
                row=1,
                col=1,
            )
            indices.append(len(fig.data) - 1)
        shapes = []
        for condition in scenario.conditions:
            shapes.append(
                dict(
                    type="line",
                    xref="x",
                    yref="y",
                    x0=waves[0].pivot.index,
                    x1=future,
                    y0=condition.price,
                    y1=condition.price,
                    line=dict(
                        color="#ce5362" if condition.kind == "INVALIDATION" else "#578ea7",
                        dash="dash",
                    ),
                )
            )
        for zone in scenario.target_zones:
            shapes.append(
                dict(
                    type="rect",
                    xref="x",
                    yref="y",
                    x0=len(bars) - 1,
                    x1=future,
                    y0=zone.low,
                    y1=zone.high,
                    fillcolor="rgba(62,172,129,0.16)",
                    line=dict(width=0),
                )
            )
        trace_groups.append(indices)
        shape_groups.append(shapes)
    buttons = []
    for scenario, group, shapes in zip(result.scenarios, trace_groups, shape_groups, strict=True):
        buttons.append(
            dict(
                label=f"{scenario.stage} | quality {scenario.score.quality_score:.0f}",
                method="update",
                args=[
                    {"visible": [i < base_count or i in group for i in range(len(fig.data))]},
                    {"shapes": shapes},
                ],
            )
        )
    ticks = list(range(0, len(bars), max(1, len(bars) // 10)))
    fig.update_layout(
        title=f"{result.instrument.canonical_symbol} · {result.timeframe} · {result.market_asof} · {result.feed} · {bundle.provenance.provider}",
        template="plotly_white",
        height=900,
        xaxis_rangeslider_visible=False,
        shapes=shape_groups[0] if shape_groups else [],
        updatemenus=[dict(buttons=buttons, x=0, y=1.12, direction="down")] if buttons else [],
        legend=dict(orientation="h", y=-0.10),
        margin=dict(t=115, b=90),
    )
    fig.update_xaxes(
        range=[max(0, len(bars) - 600), future],
        tickvals=ticks,
        ticktext=[bar_label(bars[i], result) for i in ticks],
    )
    fig.update_xaxes(title_text="K-line index (future bar offsets are illustrative)", row=3, col=1)
    return fig


def svg_chart(result: AnalysisResult, bundle: MarketDataBundle, scenario_index=0, *, png=False):
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "eliotbot-mpl"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bars = bundle.bars
    indicators = calculate(bars)
    first = max(0, len(bars) - 600)
    fig, axes = plt.subplots(
        3, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [6.5, 1.5, 2]}
    )
    for i in range(first, len(bars)):
        bar = bars[i]
        color = "#27896e" if bar.close >= bar.open else "#c55864"
        axes[0].vlines(i, bar.low, bar.high, color=color, linewidth=0.8)
        axes[0].add_patch(
            Rectangle(
                (i - 0.32, min(bar.open, bar.close)),
                0.64,
                max(abs(bar.close - bar.open), 0.005),
                color=color,
            )
        )
        axes[1].bar(i, bar.volume, color=color, width=0.7)
    x = list(range(first, len(bars)))
    axes[2].plot(x, indicators.dif[first:], label="DIF")
    axes[2].plot(x, indicators.dea[first:], label="DEA")
    axes[2].bar(x, indicators.histogram[first:], alpha=0.35)
    future = len(bars) + 25
    if result.scenarios:
        scenario = result.scenarios[scenario_index]
        waves = scenario.completed_waves
        axes[0].plot(
            [w.pivot.index for w in waves],
            [w.pivot.price for w in waves],
            "o-",
            color="#397eca",
            linewidth=2,
        )
        for wave in waves:
            axes[0].annotate(
                f"({wave.label})",
                (wave.pivot.index, wave.pivot.price),
                xytext=(0, 8),
                textcoords="offset points",
            )
        tip = scenario.developing_tip
        axes[0].plot(
            [waves[-1].pivot.index, tip.index],
            [waves[-1].pivot.price, tip.price],
            "--o",
            color="#b98137",
        )
        for condition in scenario.conditions:
            axes[0].axhline(
                condition.price,
                linestyle=":",
                color="#c95d64" if condition.kind == "INVALIDATION" else "#6c8f9a",
                alpha=0.7,
            )
        for zone in scenario.target_zones:
            axes[0].fill_between(
                [len(bars) - 1, future], zone.low, zone.high, color="#6ec59c", alpha=0.25
            )
        if scenario.projected_path:
            point = scenario.projected_path[0]
            future = max(future, len(bars) - 1 + point.reference_bars)
            axes[0].plot(
                [len(bars) - 1, len(bars) - 1 + point.reference_bars],
                [result.latest_price, point.target_price],
                ":",
                color="#379e78",
            )
    for axis in axes:
        axis.grid(alpha=0.15)
    axes[0].set_title(
        f"{result.instrument.canonical_symbol} | {bundle.provenance.provider} | {result.market_asof} | {result.status}"
    )
    axes[1].set_ylabel("Volume")
    axes[2].legend(loc="upper left")
    ticks = list(range(first, len(bars), max(1, (len(bars) - first) // 8)))
    axes[2].set_xticks(ticks, [bar_label(bars[i], result) for i in ticks], rotation=25, ha="right")
    axes[2].set_xlabel("K-line index; dotted path is conditional, not calibrated")
    axes[0].set_xlim(first - 1, future)
    fig.tight_layout()
    stream = io.BytesIO()
    fig.savefig(
        stream, format="png" if png else "svg", metadata={"Date": None} if not png else None
    )
    plt.close(fig)
    return stream.getvalue()


def render_html(result: AnalysisResult, bundle: MarketDataBundle, renderer="plotly") -> str:
    if renderer == "plotly":
        return build_plotly(result, bundle).to_html(include_plotlyjs=True, full_html=True)
    choices, panels = [], []
    count = max(1, len(result.scenarios))
    for i in range(count):
        label = result.scenarios[i].name if result.scenarios else "No qualified scenario"
        choices.append(f'<option value="{i}">{html.escape(label)}</option>')
        svg = svg_chart(result, bundle, i).decode()
        svg = svg[svg.index("<svg") :]
        panels.append(
            f'<div class="panel" style="display:{"block" if i == 0 else "none"}">{svg}</div>'
        )
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>EliotBot {html.escape(result.instrument.canonical_symbol)}</title>
<style>body{{font:15px system-ui;margin:24px;background:#f7f9fb;color:#1c3046}}.panel{{overflow:auto}}svg{{width:100%;height:auto}}select{{padding:10px}}code{{overflow-wrap:anywhere}}</style>
<h1>{html.escape(result.instrument.canonical_symbol)} · {html.escape(bundle.provenance.provider)}</h1>
<p>周期 {result.timeframe} · 行情截至 {result.market_asof} · {html.escape(result.feed)} · {html.escape(result.adjustment)}。候选权重不是成功概率。</p>
<select aria-label="切换剧本" onchange="document.querySelectorAll('.panel').forEach((p,i)=>p.style.display=i==this.value?'block':'none')">{"".join(choices)}</select>
<label>缩放 <input aria-label="图表缩放" type="range" min="100" max="300" value="100" oninput="document.querySelectorAll('svg').forEach(s=>s.style.width=this.value+'%')"></label>
{"".join(panels)}<p>输入快照：<code>{result.input_snapshot_id}</code></p></html>"""
