"""Markdown and atomic report publication from the canonical JSON result."""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from elliottwaver.errors import ExportError
from elliottwaver.schemas.market import MarketDataBundle
from elliottwaver.schemas.wave import AnalysisResult
from elliottwaver.util import atomic_write, canonical_json, digest
from elliottwaver.visualization.brief_formatter import structure_evidence
from elliottwaver.visualization.chart_builder import (
    PLOTLY_RENDER_VERSION,
    build_plotly,
    render_html,
    svg_chart,
)
from elliottwaver.visualization.report_index import catalog_metadata


def _parent_summary(scenario) -> str:
    if scenario.parent_context == "UNKNOWN":
        return "UNKNOWN"
    return f"{scenario.parent_context}/{scenario.parent_degree}/{scenario.parent_wave_label}"


def markdown_report(result: AnalysisResult) -> str:
    lines = [
        f"# ElliottWaver · {result.instrument.canonical_symbol}",
        "",
        f"行情截止：{result.market_asof}；周期：{result.timeframe}；来源：{result.feed}；调整口径：{result.adjustment}。",
        f"数据模式：`{result.mode}`；决策截止：`{result.as_of.isoformat()}`。",
        *(
            ["品种：指数；指数无复权，价格为原始点位。"]
            if result.instrument.asset_type == "INDEX"
            else []
        ),
        f"分析状态：**{result.status}**；结构歧义：{result.ambiguity}；分析价格：{result.latest_price:.4f}。",
        "",
        "候选权重仅表示本次枚举候选之间的相对评分，统计成功概率尚未校准。",
        "",
        "| 剧本 | 浪级 | 父级 | 当前阶段 | 子浪 | 质量分 | 证据覆盖 | 候选权重 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, scenario in enumerate(result.scenarios, 1):
        lines.append(
            f"| S{i} | {scenario.degree} | {_parent_summary(scenario)} | {scenario.name} | {scenario.subwave_status} | {scenario.score.quality_score:.1f}/100 | {scenario.score.coverage:.1%} | {scenario.relative_weight:.1%} |"
        )
    if not result.scenarios:
        lines += ["", "当前没有达到质量与证据门槛的候选，不强行给出数浪结论。"]
    excluded = [w for w in result.warnings if w.startswith("EXCLUDED_STRUCTURE:")]
    if excluded:
        lines += [
            "",
            "候选权重仅在通过质量/覆盖门槛的候选之间归一化；硬规则通过但评分未达标的候选见下方 `EXCLUDED_STRUCTURE`。",
        ]
        if result.scenarios and not result.scenarios[0].target_zones:
            with_targets = [
                i for i, scenario in enumerate(result.scenarios, 1) if scenario.target_zones
            ]
            if with_targets:
                label = "、".join(f"S{i}" for i in with_targets)
                lines.append(f"S1 当前无合法目标；{label} 提供条件性目标，可在图表中切换。")
    lines += ["", f"未展示的已枚举候选权重：{result.omitted_weight:.1%}。"]
    for i, scenario in enumerate(result.scenarios, 1):
        lines += [
            "",
            f"## S{i} · {scenario.name}",
            "",
            f"剧本 ID：`{scenario.scenario_id}`；下一段方向：{scenario.next_leg_bias}。",
            "",
            (
                f"父级上下文：{scenario.parent_context}；关系：{scenario.parent_relation}；"
                f"父级模板：{scenario.parent_pattern or '—'}；父级浪级：{scenario.parent_degree or '—'}；"
                f"父级浪段：{scenario.parent_wave_label or '—'}；父候选：`{scenario.parent_scenario_id or '—'}`。"
            ),
            f"子浪认证：{scenario.subwave_status}。",
            "",
            "### 关键条件",
            "",
        ]
        for condition in scenario.conditions:
            lines.append(
                f"- {condition.kind}：{condition.side} {condition.op} **{condition.price:.4f}**，{condition.reason}。"
            )
        for zone in scenario.target_zones:
            lines.append(
                f"- {zone.label}：**{zone.low:.4f}–{zone.high:.4f}**；来源：{', '.join(zone.methods)}；独立方法数 {zone.independent_method_count}。"
            )
        if scenario.subwave_evidence:
            lines += [
                "",
                "### 子浪认证",
                "",
                "| 腿 | 状态 | 子模板 | 点数 | 尺度 | 深度 | 说明 |",
                "| --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
            for item in scenario.subwave_evidence:
                scale = f"{item.scale:g}" if item.scale is not None else "—"
                lines.append(
                    f"| {item.leg} | {item.status} | {item.pattern or '—'} | {item.point_count} | {scale} | {item.depth} | {item.reason} |"
                )
        lines += [
            "",
            "### 得分依据",
            "",
            "| 特征 | 状态 | 实测值 | 匹配度 | 说明 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in scenario.score.evidence:
            value = f"{item.value:.4f}" if item.value is not None else "—"
            lines.append(
                f"| {item.feature} | {item.status} | {value} | {item.fit:.1%} | {item.explanation} |"
            )
        lines += ["", "### 硬规则", ""]
        for rule in scenario.rules:
            lines.append(f"- {rule.rule_id}：{rule.status}，{rule.reason}。")
        if scenario.structure is not None:
            lines += [
                "",
                "### 子浪证据",
                "",
                *(f"- {line}" for line in structure_evidence(scenario.structure)),
            ]
        lines += ["", *[f"- {warning}" for warning in scenario.warnings]]
    lines += [
        "",
        "## 数据与搜索状态",
        "",
        *[f"- {w}" for w in result.warnings],
        f"- 扩展状态：{result.diagnostics.expansions}；截断：{result.diagnostics.search_truncated}。",
        f"- 输入快照：`{result.input_snapshot_id}`。",
        f"- 业务结果：`{result.result_digest}`。",
        f"- 引擎：`{result.engine_version}`。",
        "",
    ]
    return "\n".join(lines)


def publish_report(
    result: AnalysisResult,
    bundle: MarketDataBundle,
    root: Path,
    formats=("html", "md"),
    renderer="plotly",
) -> Path:
    formats = tuple(sorted(set(formats)))
    if set(formats) - {"html", "md", "png"}:
        raise ExportError("Unsupported report format")
    if result.input_snapshot_id != bundle.snapshot_id:
        raise ExportError("Report input snapshot does not match the analyzed input")
    parent = root / result.instrument.canonical_symbol / "archive" / "analysis"
    if renderer not in {"plotly", "svg"}:
        raise ExportError("Unsupported renderer; choose plotly or svg")
    render_identity = [result.result_digest, formats, renderer, result.mode]
    if renderer == "plotly" and ("html" in formats or "png" in formats):
        render_identity.append(PLOTLY_RENDER_VERSION)
    destination = parent / f"{result.market_asof}_{digest(render_identity)}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".report-", dir=destination.parent))
    try:
        atomic_write(staging / "analysis.json", canonical_json(result))
        if "md" in formats:
            atomic_write(staging / "report.md", markdown_report(result))
        if "html" in formats:
            atomic_write(staging / "chart.html", render_html(result, bundle, renderer))
        if "png" in formats:
            if renderer == "svg":
                atomic_write(staging / "chart.png", svg_chart(result, bundle, png=True))
            else:
                build_plotly(result, bundle).write_image(str(staging / "chart.png"), scale=2)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.iterdir()}
        atomic_write(
            staging / "manifest.json",
            canonical_json(
                {
                    "schema_version": "1.0",
                    "input_snapshot_id": bundle.snapshot_id,
                    "result_digest": result.result_digest,
                    "renderer": renderer,
                    "catalog": catalog_metadata(
                        timeframe=result.timeframe,
                        feed=result.feed,
                        mode=result.mode,
                        status=result.status,
                        market_asof=result.market_asof,
                        bar_end=bundle.bars[-1].end_at,
                        as_of=result.as_of,
                    ),
                    "files": files,
                }
            ),
        )
        if destination.exists():
            manifest = json.loads((destination / "manifest.json").read_text())
            if (
                manifest.get("schema_version") != "1.0"
                or manifest.get("result_digest") != result.result_digest
                or manifest.get("input_snapshot_id") != bundle.snapshot_id
                or manifest.get("renderer") != renderer
                or set(manifest.get("files", {})) != set(files)
            ):
                raise ExportError("Existing report has a mismatched business identity")
            for filename, checksum in manifest["files"].items():
                if (
                    filename not in files
                    or hashlib.sha256((destination / filename).read_bytes()).hexdigest() != checksum
                ):
                    raise ExportError("Existing report failed integrity verification")
            recorded = AnalysisResult.model_validate_json(
                (destination / "analysis.json").read_text()
            )
            actual_digest = digest(
                recorded.model_dump(
                    mode="json", exclude={"run_id", "result_digest", "analyzed_at", "mode"}
                )
            )
            if actual_digest != result.result_digest or recorded.mode != result.mode:
                raise ExportError(
                    "Existing report contents do not match its declared result identity"
                )
        else:
            os.replace(staging, destination)
        if result.status in {"OK", "UNCLEAR", "SEARCH_LIMITED"}:
            pointer = (
                parent / f"latest_{result.timeframe}_{result.config_hash[:12]}_{result.feed}.json"
            )
            previous = json.loads(pointer.read_text()) if pointer.exists() else {}
            if previous.get("market_asof", "") <= str(result.market_asof):
                atomic_write(
                    pointer,
                    canonical_json(
                        {
                            "schema_version": "1.0",
                            "market_asof": result.market_asof,
                            "result_digest": result.result_digest,
                            "path": str(destination.relative_to(parent)),
                        }
                    ),
                )
        return destination
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(
            f"Report export failed ({type(exc).__name__}); no incomplete report was published"
        ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
