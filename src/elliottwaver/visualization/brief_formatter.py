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

from elliottwaver.brief import BriefRun
from elliottwaver.errors import DataError, ExportError
from elliottwaver.schemas.brief import Brief, BriefCard, forecast_message
from elliottwaver.schemas.wave import AnalysisResult, WaveScenario, WaveStructure
from elliottwaver.util import atomic_write, canonical_json, digest
from elliottwaver.visualization.brief_text import (
    DEGREE,
    METRIC_NOTE,
    PATTERN,
    adjustment_text,
    anchor_text,
    candidate_title,
    card_role,
    comparison_text,
    condition_text,
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
    timeframe_text,
)
from elliottwaver.visualization.chart_builder import PLOTLY_RENDER_VERSION, render_html

BRIEF_TEXT_RENDER_VERSION = "brief-render-v6"


def rule_evidence(scenario: WaveScenario) -> tuple[str, ...]:
    """Render the hard-rule evidence already recorded for a candidate."""
    labels = {"PASS": "通过", "FAIL": "违反", "PENDING": "证据不足"}
    price_rules = {
        "C_NEAR_A",
        "C_PROGRESS",
        "TRIANGLE_NET_RETRACEMENT",
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


def structure_evidence(structure: WaveStructure) -> tuple[str, ...]:
    nodes = {node.node_id: node for node in structure.nodes}
    root = nodes.get(structure.root_id)
    if root is None:
        return ("没有找到这套浪形的结构证据",)
    reasons = {
        "ALL_ALLOWED_STRUCTURES_FAILED": "可用子浪均未通过规则",
        "BUDGET_EXHAUSTED": "搜索达到上限",
        "DEPTH_DISABLED": "未开启更深层的子浪验证",
        "DEVELOPING_LEG": "这一浪仍在形成",
        "EMPTY_INTERVAL": "该浪没有足够的行情区间",
        "INCOMPLETE_TEMPLATE_COVERAGE": "现有浪型无法覆盖这段结构",
        "INSUFFICIENT_FINER_PIVOTS": "细一级的已确认拐点不足",
        "NO_ALLOWED_TEMPLATE": "未找到适用的子浪模板",
        "UNAVAILABLE_BAR_IN_INTERVAL": "浪内缺少必要 K 线",
        "UNCONFIRMED_BOUNDARY": "端点尚未确认",
        "WXY_SUBDIVISION_UNVERIFIED": "组合调整的子浪尚未确认",
    }
    lines = [
        f"{PATTERN.get(root.variant or '', PATTERN[root.pattern_type])} · "
        f"{status_text(structure.status)} · 子浪已验证 {structure.verified_depth} 层，"
        f"检查深度为 {structure.requested_depth} 层"
    ]

    def visit(node, path: str) -> None:
        for leg in node.child_links:
            label = f"{path}{leg.wave_label}"
            child = nodes.get(leg.child_node_id)
            if child is None:
                evidence = reasons.get(leg.reason_code or "", "等待结构证据")
                if leg.failed_rule_ids:
                    evidence += f" · {len(leg.failed_rule_ids)} 项规则未通过"
            else:
                prices = " → ".join(f"{point.pivot.price:.4f}" for point in child.points)
                pattern = PATTERN.get(child.variant or "", PATTERN[child.pattern_type])
                evidence = f"{pattern} · {prices} · {status_text(child.status)}"
            lines.append(f"浪 {label} · {status_text(leg.status)} · {evidence}")
            if child is not None and path.count(".") + 1 < structure.requested_depth:
                visit(child, f"{label}.")

    visit(root, "")
    return tuple(lines)


def relation_evidence(brief: Brief) -> tuple[str, ...]:
    daily = {card.scenario_id: card_role(index) for index, card in enumerate(brief.cards)}
    background_cards = (
        brief.background.cards if brief.background else brief.weekly.cards if brief.weekly else ()
    )
    background = {card.scenario_id: card_role(index) for index, card in enumerate(background_cards)}
    parent_label = (
        timeframe_text(brief.background.timeframe)
        if brief.background
        else timeframe_text(brief.cross_timeframe.background_timeframe)
        if brief.cross_timeframe and brief.cross_timeframe.background_timeframe
        else "周线"
    )
    child_label = timeframe_text(brief.timeframe)
    lines = []
    for item in brief.wave_relations:
        parent = background.get(item.weekly_scenario_id or "", "其他候选")
        child = (
            daily.get(item.daily_scenario_id, "其他候选") if item.daily_scenario_id else "历史区间"
        )
        reason = item.reason.removeprefix("SEARCH_LIMITED: ")
        for key, value in PATTERN.items():
            reason = reason.replace(key, value)
        wave = f"浪 {item.parent_wave_label}" if item.parent_wave_label else "浪段待定"
        lines.append(
            f"{parent_label}{parent} · {wave} · {child_label}{child}"
            f" · {status_text(item.status)} · {reason}"
        )
    return tuple(lines)


def transition_evidence(brief: Brief) -> tuple[str, ...]:
    if brief.previous is None:
        return ()
    kinds = {
        "CONFIRMATION": "确认条件",
        "INVALIDATION": "失效条件",
        "STAGE_EXIT": "重新数浪条件",
        "TARGET": "目标区",
    }
    recorded = tuple(
        f"{event['session']} · {kinds.get(event.get('kind'), '价格触及记录')} · "
        f"{status_text(event['status'])}"
        for event in brief.previous.events
        if event.get("type") in {"condition", "outcome"}
    )
    return recorded or tuple(
        condition_text(condition) for condition in brief.previous.triggered_conditions
    )


def _other_structures(brief: Brief, result: AnalysisResult | None) -> tuple[str, ...]:
    cards = {card.scenario_id for card in brief.cards}
    return (
        tuple(
            line
            for scenario in result.scenarios
            if scenario.scenario_id not in cards and scenario.structure
            for line in structure_evidence(scenario.structure)
        )
        if result is not None
        else ()
    )


def _card_metrics(card: BriefCard) -> str:
    parent = f"父浪关系{status_text(card.parent_context)}"
    if card.parent_pattern is not None and card.parent_wave_label is not None:
        parent += f"（{PATTERN[card.parent_pattern]}的浪 {card.parent_wave_label}）"
    attributes = [
        DEGREE[card.degree],
        parent,
        f"子浪{status_text(card.subwave_status)}",
        f"质量分 {card.quality_score:.1f}/100",
        f"证据覆盖 {card.coverage:.0%}",
        f"候选权重 {card.relative_weight:.1%}",
    ]
    return " · ".join(attributes)


def _window_details(brief: Brief) -> tuple[str, ...]:
    window = brief.window
    lines = [
        f"决策截止 {brief.as_of.isoformat()}",
        f"分析范围 {window.start} 至 {window.end} · {window.bar_count} 根 K 线",
    ]
    if window.start_at is not None and window.end_at is not None:
        lines.append(
            f"精确范围 {market_time_text(window.start_at, brief.symbol)} 至 "
            f"{market_time_text(window.end_at, brief.symbol)}"
        )
    if brief.last_bar_end_at is not None:
        available = (
            market_time_text(brief.last_bar_available_at, brief.symbol)
            if brief.last_bar_available_at is not None
            else "未记录"
        )
        lines.append(
            f"最后一根已完成 K 线 {market_time_text(brief.last_bar_end_at, brief.symbol)}"
            f" · 可用 {available}"
        )
    if window.primary_pattern_start:
        lines.append(f"首选浪形起始于 {window.primary_pattern_start}")
    if (comparison := brief.window_comparison) and comparison.alternate_start:
        lines.append(f"另用 {comparison.alternate_start} 作为起点复核")
    lines.extend(anchor_text(anchor) for anchor in window.anchor_details)
    return tuple(lines)


def _display_issues(brief: Brief) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(diagnostic_text(issue.code, issue.message) for issue in brief.issues)
    )


def _md_items(lines: tuple[str, ...]) -> list[str]:
    return [f"- {line}" for line in lines]


def brief_markdown(brief: Brief, result: AnalysisResult | None = None) -> str:
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    title = "盘中简报" if brief.timeframe.endswith("m") else "收盘摘要"
    lines = [
        f"# {brief.symbol} · {title}",
        "",
        f"{timeframe_text(brief.timeframe)} · 行情截至 {brief.market_asof} · {status_text(brief.status)}",
        f"{mode_text(brief.mode)} · {feed_text(brief.feed)} · {adjustment_text(brief.adjustment)}",
        f"分析价格 {price_text(brief.latest_price)}",
        "",
    ]
    if (heading := outlook_heading(brief)) is not None:
        lines += [f"## {heading}", ""]
    for title, content in summary_cards(brief):
        lines += [f"### {title}", "", *_md_items(content), ""]
    if not brief.cards:
        lines += ["当前没有达到规则和证据要求的候选。", ""]
    lines += [*_md_items(comparison_text(brief)), "", forecast_message(brief.forecast_status), ""]
    if brief.retrospective:
        lines += ["模型结果为回顾性重算。", ""]
    if brief.forecasts:
        lines += [
            "| 事件 | 候选 | 期限 | 状态 | 概率 |",
            "| --- | --- | --- | --- | --- |",
            *("| " + " | ".join(forecast_row(brief, item)) + " |" for item in brief.forecasts),
            "",
        ]
    lines += ["## 核对依据", "", *_md_items(_window_details(brief)), ""]
    if brief.cards:
        lines += [METRIC_NOTE, ""]
        if omitted := omission_text(brief):
            lines += [omitted, ""]
    for index, card in enumerate(brief.cards):
        lines += [
            f"### {card_role(index)} · {candidate_title(card)}",
            "",
            _card_metrics(card),
            "",
        ]
        if card.structure is not None:
            lines += [*_md_items(structure_evidence(card.structure)), ""]
        if scenario := scenarios.get(card.scenario_id):
            lines += ["#### 规则复核", "", *_md_items(rule_evidence(scenario)), ""]
    if brief.weekly is not None:
        weekly = brief.weekly
        lines += [
            "### 周线背景",
            "",
            f"行情截至 {weekly.market_asof} · {status_text(weekly.status)}",
            "",
            *(
                f"- {card_role(index)} · {candidate_title(card)}"
                for index, card in enumerate(weekly.cards)
            ),
            "",
        ]
    evidence = (
        ("日周父子浪", relation_evidence(brief)),
        ("历史条件触发记录", transition_evidence(brief)),
        ("其他候选的结构证据", _other_structures(brief, result)),
        ("数据与搜索提示", _display_issues(brief)),
    )
    for title, items in evidence:
        if items:
            lines += [f"### {title}", "", *_md_items(items), ""]
    lines += [
        "原始代码、精确分析字段和快照标识可查 [摘要 JSON](brief.json) 与 [规则 JSON](daily.json)。",
        "",
    ]
    return "\n".join(lines)


def brief_html(brief: Brief, result: AnalysisResult | None = None) -> str:
    scenarios = {item.scenario_id: item for item in result.scenarios} if result else {}
    heading = outlook_heading(brief)
    outlook = f"<h2>{html.escape(heading)}</h2>" if heading else ""
    cards = "".join(
        f"<section class='card'><h3>{html.escape(title)}</h3>{_item_list(lines)}</section>"
        for title, lines in summary_cards(brief)
    )
    if not cards:
        cards = "<p>当前没有达到规则和证据要求的候选。</p>"
    comparisons = "".join(f"<p>{html.escape(line)}</p>" for line in comparison_text(brief))
    retrospective = "<p>模型结果为回顾性重算。</p>" if brief.retrospective else ""
    forecasts = ""
    if brief.forecasts:
        rows = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(cell)}</td>" for cell in forecast_row(brief, item))
            + "</tr>"
            for item in brief.forecasts
        )
        forecasts = (
            "<h2>概率结果</h2><table><thead><tr><th>事件</th><th>候选</th><th>期限</th>"
            f"<th>状态</th><th>概率</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    evidence = [_evidence_details("分析范围和起点", _window_details(brief))]
    for index, card in enumerate(brief.cards):
        lines = _evidence_details(
            "子浪证据", structure_evidence(card.structure) if card.structure is not None else ()
        )
        if scenario := scenarios.get(card.scenario_id):
            lines += _evidence_details("规则复核", rule_evidence(scenario))
        title = f"{card_role(index)} · {candidate_title(card)}"
        evidence.append(
            f"<section><h3>{html.escape(title)}</h3><p>{html.escape(_card_metrics(card))}</p>{lines}</section>"
        )
    if weekly := brief.weekly:
        evidence.append(
            _evidence_details(
                "周线背景",
                (
                    f"行情截至 {weekly.market_asof} · {status_text(weekly.status)}",
                    *(
                        f"{card_role(index)} · {candidate_title(card)}"
                        for index, card in enumerate(weekly.cards)
                    ),
                ),
            )
        )
    evidence.extend(
        (
            _evidence_details("日周父子浪", relation_evidence(brief)),
            _evidence_details("历史条件触发记录", transition_evidence(brief)),
            _evidence_details("其他候选的结构证据", _other_structures(brief, result)),
            _evidence_details("数据与搜索提示", _display_issues(brief)),
        )
    )
    metrics = f"<p>{METRIC_NOTE}</p>" if brief.cards else ""
    return f"""<!doctype html>
<html lang="zh"><meta charset="utf-8">
<title>{html.escape(brief.symbol)} · ElliottWaver 收盘摘要</title>
<style>
body{{font:15px/1.55 system-ui,sans-serif;margin:0;background:#f4f7fa;color:#1b2b3a}}
main{{max-width:1100px;margin:24px auto;padding:0 18px}}
header{{background:#12324a;color:#fff;padding:22px;border-radius:10px}}
.card{{background:#fff;border:1px solid #d8e2ea;border-radius:10px;padding:16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #d8e2ea;padding:8px;text-align:left}}
</style>
<main><header><h1>{html.escape(brief.symbol)} · 收盘摘要</h1>
<p>{html.escape(timeframe_text(brief.timeframe))} · 行情截至 {brief.market_asof} ·
{html.escape(status_text(brief.status))}</p>
<p>{html.escape(mode_text(brief.mode))} · {html.escape(feed_text(brief.feed))} ·
{html.escape(adjustment_text(brief.adjustment))}</p>
<p>分析价格 {price_text(brief.latest_price)}</p></header>
{outlook}{cards}{comparisons}{retrospective}
<p>{html.escape(forecast_message(brief.forecast_status))}</p>{forecasts}
<p><a href="daily.html">打开 K 线图</a></p>
<h2>核对依据</h2>{metrics}<p>{html.escape(omission_text(brief))}</p>
{"".join(evidence)}
<p><a href="brief.json">原始摘要 JSON</a> · <a href="daily.json">分析与规则 JSON</a></p>
</main></html>"""


def _item_list(lines: tuple[str, ...]) -> str:
    if not lines:
        return ""
    return "<ul>" + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>"


def _evidence_details(title: str, lines: tuple[str, ...]) -> str:
    if not lines:
        return ""
    return f"<details><summary>{html.escape(title)}</summary>{_item_list(lines)}</details>"


def read_brief_report(
    directory: Path, *, expected_digest: str | None = None
) -> tuple[Brief, AnalysisResult]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        files = manifest["files"]
        required = {"brief.json", "daily.json"}
        allowed = required | {"weekly.json", "report.md", "index.html", "daily.html", "weekly.html"}
        if (
            manifest["schema_version"] not in {"1.0", "1.1", "1.2"}
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
    render_identity = [
        run.brief.brief_digest,
        formats,
        renderer,
        BRIEF_TEXT_RENDER_VERSION if formats else "brief-render-v2",
    ]
    if renderer == "plotly" and "html" in formats:
        render_identity.append(PLOTLY_RENDER_VERSION)
    destination = (
        root
        / run.result.instrument.market
        / run.brief.symbol
        / "brief"
        / str(run.brief.market_asof)
        / digest(render_identity)
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
                    "schema_version": "1.2" if run.brief.schema_version == "1.2" else "1.1",
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
