"""Markdown/HTML publication for the one-page close brief."""

from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import shutil
import tempfile
from pathlib import Path

from eliotbot.brief import BriefRun
from eliotbot.errors import DataError, ExportError
from eliotbot.schemas.brief import Brief, BriefPeriod, forecast_message
from eliotbot.schemas.wave import AnalysisResult, WaveScenario
from eliotbot.util import atomic_write, canonical_json, digest
from eliotbot.visualization.chart_builder import render_html


def _parent(card) -> str:
    if card.parent_context == "UNKNOWN":
        return "UNKNOWN"
    return (
        f"{card.parent_context}/{card.parent_pattern or '—'}/"
        f"{card.parent_degree or '—'}/{card.parent_wave_label or '—'}"
    )


def _anchor_text(anchor):
    reason = {
        "EXPLICIT": "手动起点",
        "FALLBACK": "历史窗口回退",
        "MACRO_HIGH": "背景高点",
        "MACRO_LOW": "背景低点",
    }[anchor.origin]
    context = (
        f" · {anchor.background_timeframe} · 确认时刻 {anchor.confirmed_at}"
        if anchor.confirmed_at
        else ""
    )
    return f"{anchor.date} · {reason}{context}"


def _forecast_evidence(item):
    scope = item.scope or {}
    fields = [
        str(value)
        for name in ("markets", "asset_types", "timeframes", "feed")
        for value in scope.get(name, [])
    ]
    if item.sample_count is not None:
        fields.append(f"留出 {item.sample_count} 条")
    if item.calibration_end is not None:
        fields.append(f"校准截至 {item.calibration_end}")
    return " · ".join(fields) or "—"


def omission_text(period: Brief | BriefPeriod) -> str:
    engine = f"引擎 Top-K 之外的候选权重 {period.omitted_weight:.1%}"
    if period.omitted_top_k_count is None or period.omitted_top_k_weight is None:
        return engine
    return (
        f"{engine}。摘要另省略 Top-K 内 {period.omitted_top_k_count} 个候选"
        f"（权重 {period.omitted_top_k_weight:.1%}）。"
    )


def rule_evidence(scenario: WaveScenario) -> tuple[str, ...]:
    """Render the hard-rule evidence already recorded for a candidate."""
    labels = {"PASS": "通过", "FAIL": "违反", "PENDING": "证据不足"}
    price_rules = {
        "C_NEAR_A",
        "C_PROGRESS",
        "W2_OR_B_ORIGIN",
        "W3_PROGRESS",
        "W4_NO_FULL_RETRACE",
        "W4_NO_OVERLAP",
        "W4_OVERLAP",
        "W5_LENGTH_CAP",
        "W5_PROGRESS",
        "X_ORIGIN",
        "Y_BEYOND_W",
    }
    lines = []
    for rule in scenario.rules:
        text = f"{labels[rule.status]} · {rule.reason}"
        sign = -1 if scenario.pattern_direction == "DOWN" and rule.rule_id in price_rules else 1
        if rule.observed is not None:
            text += f" · 实测 {sign * rule.observed:.4f}"
        if rule.boundary is not None:
            text += f" · 判定边界 {sign * rule.boundary:.4f}"
        lines.append(text)
    return tuple(lines)


def brief_markdown(brief: Brief, result: AnalysisResult | None = None) -> str:
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    lines = [
        f"# {brief.symbol} · 收盘摘要",
        "",
        (
            f"行情截止：{brief.market_asof}；周期：{brief.timeframe}；"
            f"来源：{brief.feed}；调整口径：{brief.adjustment}。"
        ),
        f"分析状态：**{brief.status}**；分析价格：{brief.latest_price:.4f}。",
        f"数据模式 `{brief.mode}` · 决策截止 `{brief.as_of.isoformat()}`。",
        f"周期选择：{brief.timeframe_reason or '—'}。",
        "",
        (
            f"分析窗口：{brief.window.start} → {brief.window.end}，"
            f"{brief.window.bar_count} 根；锚点："
            + (", ".join(str(day) for day in brief.window.anchors) or "—")
        ),
        (
            f"首选结构起点：{brief.window.primary_pattern_start or '—'}；"
            f"锚点回退：{'是' if brief.window.fallback else '否'}。"
        ),
        "",
    ]
    lines.extend(f"- {_anchor_text(anchor)}" for anchor in brief.window.anchor_details)
    if brief.retrospective:
        lines += ["", "**回顾性重算**。使用显式指定的模型。", ""]
    if brief.cross_timeframe is not None:
        cross = brief.cross_timeframe
        lines += [
            f"跨周期：**{cross.state}**；日线下一段 {cross.primary_bias}；"
            f"周线下一段 {cross.weekly_bias or '—'}。{cross.reason}",
            "",
        ]
    if brief.window_comparison is not None:
        window = brief.window_comparison
        lines += [
            f"换窗复核：**{window.state}**；替代起点 {window.alternate_start or '—'}；"
            f"根数 {window.alternate_bar_count or '—'}。{window.reason}",
            "",
        ]
    if brief.previous is not None:
        previous = brief.previous
        lines += [
            f"上次判断：**{previous.change}**；{previous.reason}；"
            f"上次行情截止 {previous.previous_market_asof or '—'}。",
            "",
        ]
    if brief.previous and brief.previous.triggered_conditions:
        lines += [
            "上次已触发的条件",
            *[
                f"- {cond.kind} {cond.side} {cond.op} {cond.price:.4f}"
                for cond in brief.previous.triggered_conditions
            ],
            "",
        ]
    if brief.forecasts:
        lines += [
            "| 事件 | 候选 | 状态 | 期限 | 概率 | 校准证据 |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
        for forecast in brief.forecasts:
            probabilities = (
                ", ".join(f"{name}={value:.1%}" for name, value in forecast.probabilities.items())
                if forecast.probabilities
                else forecast.reason or "—"
            )
            lines.append(
                f"| {forecast.event_id} | {forecast.scenario_id or '标的'} | {forecast.status} | "
                f"{forecast.horizon_sessions} {forecast.horizon_unit} | {probabilities} | {_forecast_evidence(forecast)} |"
            )
        lines.append("")
    lines += [
        "候选权重只表示本次枚举候选之间的相对评分，不是成功概率。",
        "",
        "| 角色 | 候选 | 浪级 | 阶段 | 父级 | 子浪 | 质量 | 覆盖 | 权重 |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for card in brief.cards:
        lines.append(
            f"| {card.role} | {card.name} | {card.degree} | {card.stage} | "
            f"{_parent(card)} | {card.subwave_status} | {card.quality_score:.1f} | "
            f"{card.coverage:.1%} | {card.relative_weight:.1%} |"
        )
    if not brief.cards:
        lines += ["", "当前没有达到质量与证据门槛的候选。"]
    lines += ["", omission_text(brief)]
    for index, card in enumerate(brief.cards, 1):
        lines += [
            "",
            f"## {index}. {card.name}",
            "",
            f"角色：{card.role}；与主选差异：{card.difference}；下一段方向：{card.next_leg_bias}。",
            "",
        ]
        for condition in card.conditions:
            lines.append(
                f"- {condition.kind}：{condition.side} {condition.op} "
                f"**{condition.price:.4f}**，{condition.reason}。"
            )
        for zone in card.target_zones:
            lines.append(
                f"- {zone.label}：**{zone.low:.4f}–{zone.high:.4f}**；"
                f"来源：{', '.join(zone.methods)}。"
            )
        if not card.conditions and not card.target_zones:
            lines.append("- 当前没有可用的确认、失效或目标条件。")
        for warning in card.warnings:
            lines.append(f"- {warning}")
        if card.scenario_id in scenarios:
            lines.extend(("", "### 规则复核", ""))
            lines.extend(f"- {line}" for line in rule_evidence(scenarios[card.scenario_id]))
    if brief.weekly is not None:
        weekly = brief.weekly
        lines += [
            "",
            "## 周线背景",
            "",
            f"状态：{weekly.status}；行情截止：{weekly.market_asof}；"
            f"窗口：{weekly.window.start} → {weekly.window.end}，{weekly.window.bar_count} 根。",
            "",
            "| 角色 | 候选 | 浪级 | 阶段 | 父级 | 子浪 | 质量 | 覆盖 | 权重 |",
            "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
        for card in weekly.cards:
            lines.append(
                f"| {card.role} | {card.name} | {card.degree} | {card.stage} | "
                f"{_parent(card)} | {card.subwave_status} | {card.quality_score:.1f} | "
                f"{card.coverage:.1%} | {card.relative_weight:.1%} |"
            )
        if not weekly.cards:
            lines.append("| — | 当前没有达到门槛的周线候选 | — | — | — | — | — | — | — |")
    lines += ["", "## 数据与搜索提示", ""]
    if brief.issues:
        for issue in brief.issues:
            lines.append(f"- `{issue.code}`（{issue.scope}）：{issue.message}")
    else:
        lines.append("- 未记录额外问题。")
    lines += [
        "",
        "## 概率状态",
        "",
        f"- {brief.forecast_status}：{forecast_message(brief.forecast_status)}",
        "",
        f"- 源快照：`{brief.source_snapshot_id}`",
        f"- 分析输入：`{brief.input_snapshot_id}`",
        f"- 业务结果：`{brief.result_digest}`",
        f"- 引擎：`{brief.engine_version}`",
        "",
    ]
    return "\n".join(lines)


def brief_html(brief: Brief, result: AnalysisResult | None = None) -> str:
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    cards = []
    for card in brief.cards:
        conditions = (
            "".join(
                f"<li>{html.escape(condition.kind)}：{html.escape(condition.side)} "
                f"{html.escape(condition.op)} <b>{condition.price:.4f}</b>，"
                f"{html.escape(condition.reason)}</li>"
                for condition in card.conditions
            )
            or "<li>当前没有可用条件。</li>"
        )
        zones = (
            "".join(
                f"<li>{html.escape(zone.label)}：<b>{zone.low:.4f}–{zone.high:.4f}</b>；"
                f"{html.escape(', '.join(zone.methods))}</li>"
                for zone in card.target_zones
            )
            or "<li>当前没有可用目标区。</li>"
        )
        review = ""
        if card.scenario_id in scenarios:
            entries = "".join(
                f"<li>{html.escape(line)}</li>"
                for line in rule_evidence(scenarios[card.scenario_id])
            )
            review = f"<details><summary>规则复核</summary><ul>{entries}</ul></details>"
        cards.append(
            f"<section class='card'><h2>{html.escape(card.role)} · "
            f"{html.escape(card.name)}</h2>"
            f"<p>浪级 {html.escape(card.degree)} · 阶段 {html.escape(card.stage)} · "
            f"父级 {html.escape(_parent(card))} · 子浪 {html.escape(card.subwave_status)}</p>"
            f"<p>质量 {card.quality_score:.1f}/100 · 覆盖 {card.coverage:.1%} · "
            f"权重 {card.relative_weight:.1%} · 差异 {html.escape(card.difference)}</p>"
            f"<h3>条件</h3><ul>{conditions}</ul><h3>目标区</h3><ul>{zones}</ul>{review}</section>"
        )
    if not cards:
        cards.append("<p>当前没有达到质量与证据门槛的候选。</p>")
    issues = (
        "".join(
            f"<li><code>{html.escape(issue.code)}</code>（{html.escape(issue.scope)}）："
            f"{html.escape(issue.message)}</li>"
            for issue in brief.issues
        )
        or "<li>未记录额外问题。</li>"
    )
    cross = ""
    if brief.cross_timeframe is not None:
        value = brief.cross_timeframe
        cross = (
            f"<p>跨周期：<b>{html.escape(value.state)}</b>；日线下一段 "
            f"{html.escape(value.primary_bias)}；周线下一段 "
            f"{html.escape(value.weekly_bias or '—')}。{html.escape(value.reason)}</p>"
        )
    window = ""
    if brief.window_comparison is not None:
        value = brief.window_comparison
        window = (
            f"<p>换窗复核：<b>{html.escape(value.state)}</b>；替代起点 "
            f"{value.alternate_start or '—'}；根数 {value.alternate_bar_count or '—'}。"
            f"{html.escape(value.reason)}</p>"
        )
    anchors = "".join(
        f"<li>{html.escape(_anchor_text(anchor))}</li>" for anchor in brief.window.anchor_details
    )
    retrospective = "<p><b>回顾性重算</b>。使用显式指定的模型。</p>" if brief.retrospective else ""
    forecasts = ""
    if brief.forecasts:
        rows = "".join(
            f"<tr><td>{html.escape(item.event_id)}</td>"
            f"<td>{html.escape(item.scenario_id or '标的')}</td><td>{html.escape(item.status)}</td>"
            f"<td>{item.horizon_sessions} {html.escape(item.horizon_unit)}</td><td>"
            + (
                html.escape(
                    ", ".join(f"{name}={value:.1%}" for name, value in item.probabilities.items())
                )
                if item.probabilities
                else html.escape(item.reason or "—")
            )
            + f"</td><td>{html.escape(_forecast_evidence(item))}</td></tr>"
            for item in brief.forecasts
        )
        forecasts = (
            "<h2>概率模型</h2><table><thead><tr><th>事件</th><th>候选</th><th>状态</th>"
            "<th>期限</th><th>概率</th><th>校准证据</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    previous = ""
    if brief.previous is not None:
        value = brief.previous
        previous = (
            f"<p>上次判断：<b>{html.escape(value.change)}</b>；"
            f"{html.escape(value.reason)}；上次行情截止 "
            f"{value.previous_market_asof or '—'}。</p>"
        )
    if brief.previous and brief.previous.triggered_conditions:
        conditions = ", ".join(
            f"{c.kind} {c.side} {c.op} {c.price:.4f}" for c in brief.previous.triggered_conditions
        )
        previous += f"<p>上次已触发的条件 · {html.escape(conditions)}</p>"
    weekly = ""
    if brief.weekly is not None:
        value = brief.weekly
        rows = (
            "".join(
                f"<tr><td>{html.escape(card.role)}</td><td>{html.escape(card.name)}</td>"
                f"<td>{html.escape(card.degree)}</td><td>{html.escape(card.stage)}</td>"
                f"<td>{html.escape(_parent(card))}</td><td>{html.escape(card.subwave_status)}</td>"
                f"<td>{card.quality_score:.1f}</td><td>{card.coverage:.1%}</td>"
                f"<td>{card.relative_weight:.1%}</td></tr>"
                for card in value.cards
            )
            or "<tr><td colspan='9'>当前没有达到门槛的周线候选</td></tr>"
        )
        weekly = (
            f"<h2>周线背景</h2><p>状态 {html.escape(value.status)} · 行情截止 "
            f"{value.market_asof} · 窗口 {value.window.start} → {value.window.end}</p>"
            "<table><thead><tr><th>角色</th><th>候选</th><th>浪级</th><th>阶段</th>"
            "<th>父级</th><th>子浪</th><th>质量</th><th>覆盖</th><th>权重</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return f"""<!doctype html>
<html lang="zh"><meta charset="utf-8">
<title>{html.escape(brief.symbol)} · EliotBot 收盘摘要</title>
<style>
body{{font:15px/1.55 system-ui,sans-serif;margin:0;background:#f4f7fa;color:#1b2b3a}}
main{{max-width:1100px;margin:24px auto;padding:0 18px}}
header{{background:#12324a;color:#fff;padding:22px;border-radius:10px}}
.card{{background:#fff;border:1px solid #d8e2ea;border-radius:10px;padding:16px;margin:14px 0}}
code{{overflow-wrap:anywhere}} .muted{{color:#5a6b7b}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #d8e2ea;padding:8px;text-align:left}}
</style>
<main><header><h1>{html.escape(brief.symbol)} · 收盘摘要</h1>
<p>行情截止 {brief.market_asof} · 周期 {html.escape(brief.timeframe)} ·
状态 {html.escape(brief.status)} · 分析价格 {brief.latest_price:.4f}</p>
<p>来源 <code>{html.escape(brief.feed)}</code> · 调整口径
<code>{html.escape(brief.adjustment)}</code></p>
<p>数据模式 <code>{html.escape(brief.mode)}</code> ·
决策截止 <time datetime="{brief.as_of.isoformat()}">{brief.as_of.isoformat()}</time></p>
<p>{html.escape(brief.timeframe_reason or "")}</p></header>
<p class="muted">候选权重不是成功概率。{html.escape(forecast_message(brief.forecast_status))}</p>
<p><a href="daily.html">打开 K 线图</a></p>
<h2>分析窗口</h2>
<p>{brief.window.start} → {brief.window.end} · {brief.window.bar_count} 根 ·
锚点 {html.escape(", ".join(str(day) for day in brief.window.anchors) or "—")} ·
首选结构起点 {brief.window.primary_pattern_start or "—"}</p>
{f"<ul>{anchors}</ul>" if anchors else ""}
{cross}{window}{previous}{retrospective}{forecasts}{weekly}
{"".join(cards)}
<p>{html.escape(omission_text(brief))}</p>
<h2>数据与搜索提示</h2><ul>{issues}</ul>
<p class="muted">源快照 <code>{html.escape(brief.source_snapshot_id)}</code> ·
分析输入 <code>{html.escape(brief.input_snapshot_id)}</code> ·
引擎 <code>{html.escape(brief.engine_version)}</code></p>
</main></html>"""


def read_brief_report(
    directory: Path, *, expected_digest: str | None = None
) -> tuple[Brief, AnalysisResult]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        files = manifest["files"]
        required = {"brief.json", "daily.json"}
        allowed = required | {"weekly.json", "report.md", "index.html", "daily.html", "weekly.html"}
        if (
            manifest["schema_version"] not in {"1.0", "1.1"}
            or not required <= set(files) <= allowed
            or {p.name for p in directory.iterdir()} != set(files) | {"manifest.json"}
        ):
            raise ValueError("report file set or version mismatch")
        payload = {}
        for name, checksum in files.items():
            data = (directory / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError(f"checksum mismatch for {name}")
            if name.endswith(".json"):
                payload[name] = json.loads(data)
        brief_raw = payload["brief.json"]
        brief = Brief.model_validate(brief_raw)
        business_digest = digest(
            {k: v for k, v in brief_raw.items() if k not in {"brief_digest", "generated_at"}}
        )
        if not business_digest == brief.brief_digest == manifest["brief_digest"]:
            raise ValueError("brief digest mismatch")
        if expected_digest is not None and brief.brief_digest != expected_digest:
            raise ValueError("report identity mismatch")
        if (
            manifest["source_snapshot_id"],
            manifest["input_snapshot_id"],
            manifest["result_digest"],
        ) != (brief.source_snapshot_id, brief.input_snapshot_id, brief.result_digest):
            raise ValueError("report input identity mismatch")

        def checked_analysis(name, expected):
            raw = payload[name]
            result = AnalysisResult.model_validate(raw)
            identity = digest(
                {
                    k: v
                    for k, v in raw.items()
                    if k not in {"run_id", "result_digest", "analyzed_at", "mode"}
                }
            )
            if (
                identity != result.result_digest
                or result.result_digest != expected.result_digest
                or result.input_snapshot_id != expected.input_snapshot_id
                or result.instrument.canonical_symbol != brief.symbol
                or result.as_of != brief.as_of
            ):
                raise ValueError(f"analysis identity mismatch for {name}")
            return result

        result = checked_analysis("daily.json", brief)
        if brief.weekly is not None:
            checked_analysis("weekly.json", brief.weekly)
        return brief, result
    except (OSError, TypeError, KeyError, ValueError, AttributeError) as exc:
        raise DataError(f"BRIEF_REPORT_INVALID: {exc}") from exc


def publish_brief(
    run: BriefRun,
    root: Path,
    formats=("html", "md"),
    renderer="plotly",
) -> Path:
    formats = tuple(sorted(set(formats)))
    if set(formats) - {"html", "md"} or renderer not in {"plotly", "svg"}:
        raise ExportError("Unsupported brief format")
    if run.result.input_snapshot_id != run.prepared.snapshot_id:
        raise ExportError("Brief result does not match the prepared input snapshot")
    destination = (
        root
        / run.result.instrument.market
        / run.brief.symbol
        / "brief"
        / str(run.brief.market_asof)
        / digest(
            [
                run.brief.brief_digest,
                formats,
                renderer,
                "brief-render-v3" if formats else "brief-render-v2",
            ]
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".brief-", dir=destination.parent))
    try:
        atomic_write(staging / "brief.json", canonical_json(run.brief))
        atomic_write(staging / "daily.json", canonical_json(run.result))
        if run.weekly_result is not None and run.weekly_prepared is not None:
            if run.weekly_result.input_snapshot_id != run.weekly_prepared.snapshot_id:
                raise ExportError("Weekly result does not match the prepared input snapshot")
            atomic_write(staging / "weekly.json", canonical_json(run.weekly_result))
        if "md" in formats:
            atomic_write(staging / "report.md", brief_markdown(run.brief, run.result))
        if "html" in formats:
            atomic_write(staging / "index.html", brief_html(run.brief, run.result))
            atomic_write(
                staging / "daily.html",
                render_html(run.result, run.prepared, renderer),
            )
            if run.weekly_result is not None and run.weekly_prepared is not None:
                atomic_write(
                    staging / "weekly.html",
                    render_html(run.weekly_result, run.weekly_prepared, renderer),
                )
        files = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in staging.iterdir()
        }
        atomic_write(
            staging / "manifest.json",
            canonical_json(
                {
                    "schema_version": "1.1",
                    "renderer": renderer,
                    "brief_digest": run.brief.brief_digest,
                    "source_snapshot_id": run.brief.source_snapshot_id,
                    "input_snapshot_id": run.brief.input_snapshot_id,
                    "result_digest": run.brief.result_digest,
                    "engine_version": run.brief.engine_version,
                    "config_hash": run.brief.config_hash,
                    "files": files,
                }
            ),
        )
        try:
            os.replace(staging, destination)
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
            read_brief_report(destination, expected_digest=run.brief.brief_digest)
            manifest = json.loads((destination / "manifest.json").read_text())
            if set(manifest["files"]) != set(files) or manifest.get("renderer") != renderer:
                raise ExportError("Existing brief representation mismatch") from exc
        return destination
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExportError(
            f"Brief export failed ({type(exc).__name__}); no incomplete brief was published"
        ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
