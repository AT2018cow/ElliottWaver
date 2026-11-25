"""Stable browser entry points for immutable report directories."""

import fcntl
import hashlib
import html
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from elliottwaver.errors import DataError
from elliottwaver.util import atomic_write
from elliottwaver.visualization.brief_text import (
    feed_text,
    market_time_text,
    mode_text,
    status_text,
    timeframe_text,
)

REPORT_KINDS = ("brief", "analysis")
ISSUANCE_PENDING = ".issuance-pending"
ISSUANCE_FILE = "issuance.json"


def report_entry(output: Path) -> Path:
    """Return the short browser entry point, also accepting existing archive paths."""
    if output.parent.name in REPORT_KINDS and output.parent.parent.name == "archive":
        index = output.parents[2] / "index.html"
        if index.is_file():
            return index
    return output


def catalog_metadata(
    *,
    timeframe: str,
    feed: str,
    mode: str,
    status: str,
    market_asof: date,
    bar_end: datetime,
    as_of: datetime,
    retrospective: bool = False,
) -> dict:
    return {
        "timeframe": timeframe,
        "feed": feed,
        "mode": mode,
        "status": status,
        "market_asof": market_asof,
        "bar_end": bar_end,
        "as_of": as_of,
        "retrospective": retrospective,
    }


@dataclass(frozen=True)
class ReportLink:
    kind: str
    timeframe: str
    feed: str
    mode: str
    status: str
    market_asof: date
    bar_end: datetime
    as_of: datetime
    retrospective: bool
    file: Path
    business_id: str
    checksum: str

    @property
    def operational(self) -> bool:
        return self.mode == "LIVE" and not self.retrospective and self.feed != "synthetic"

    @property
    def label(self) -> str:
        if self.feed == "synthetic":
            return "合成演示"
        if self.retrospective:
            return "模型回顾性重算"
        return mode_text(self.mode)


def _read_link(directory: Path, kind: str) -> ReportLink | None:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        info = manifest["catalog"]
        business_id = manifest["brief_digest" if kind == "brief" else "result_digest"]
        files = manifest["files"]
        if (
            not all(isinstance(info[key], str) for key in ("timeframe", "feed", "mode", "status"))
            or not isinstance(info["retrospective"], bool)
            or not isinstance(business_id, str)
            or not isinstance(files, dict)
        ):
            return None
        if kind == "brief" and info["mode"] == "LIVE":
            if (directory / ISSUANCE_PENDING).exists() or not (directory / ISSUANCE_FILE).is_file():
                return None
        choices = (
            ("index.html", "report.md", "brief.json")
            if kind == "brief"
            else ("chart.html", "report.md", "chart.png", "analysis.json")
        )
        file = next(
            (
                directory / name
                for name in choices
                if name in files and (directory / name).is_file()
            ),
            None,
        )
        if file is None:
            return None
        bar_end = datetime.fromisoformat(info["bar_end"])
        as_of = datetime.fromisoformat(info["as_of"])
        if bar_end.tzinfo is None or as_of.tzinfo is None:
            return None
        return ReportLink(
            kind,
            info["timeframe"],
            info["feed"],
            info["mode"],
            info["status"],
            date.fromisoformat(info["market_asof"]),
            bar_end,
            as_of,
            info["retrospective"],
            file,
            business_id,
            files[file.name],
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _verified_link(entry: ReportLink, symbol: str) -> bool:
    if entry.kind == "brief":
        # The formatter imports the shared layout metadata above.
        from elliottwaver.visualization.brief_formatter import read_brief_report

        try:
            brief, _ = read_brief_report(entry.file.parent, expected_digest=entry.business_id)
        except DataError:
            return False
        return (
            brief.symbol == symbol
            and brief.mode == entry.mode
            and brief.retrospective == entry.retrospective
            and brief.timeframe == entry.timeframe
            and brief.feed == entry.feed
            and brief.status == entry.status
            and brief.market_asof == entry.market_asof
            and brief.last_bar_end_at == entry.bar_end
            and brief.as_of == entry.as_of
        )
    try:
        return hashlib.sha256(entry.file.read_bytes()).hexdigest() == entry.checksum
    except OSError:
        return False


def _table(entries: list[ReportLink], symbol_dir: Path) -> str:
    rows = []
    for entry in entries:
        kind = "简报" if entry.kind == "brief" else "详细分析"
        period = timeframe_text(entry.timeframe)
        href = quote(entry.file.relative_to(symbol_dir).as_posix())
        data_at = (
            market_time_text(entry.bar_end, symbol_dir.name)
            if entry.timeframe.endswith("m")
            else entry.market_asof.isoformat()
        )
        fields = (
            f"{period}{kind}",
            data_at,
            entry.label,
            feed_text(entry.feed),
            status_text(entry.status),
        )
        cells = "".join(f"<td>{html.escape(value)}</td>" for value in fields)
        rows.append(f'<tr>{cells}<td><a href="{href}">打开报告</a></td></tr>')
    return (
        "<table><thead><tr><th>报告</th><th>行情截止</th><th>模式</th>"
        "<th>来源</th><th>状态</th><th>查看</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def write_report_index(symbol_dir: Path) -> Path:
    archive = symbol_dir / "archive"
    with (archive / ".index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = []
        for kind in REPORT_KINDS:
            for directory in (archive / kind).glob("[0-9]*"):
                if entry := _read_link(directory, kind):
                    entries.append(entry)
        entries.sort(
            key=lambda row: (
                row.bar_end,
                row.as_of,
                row.file.suffix == ".html",
                row.file.as_posix(),
            ),
            reverse=True,
        )
        live = {}
        other = {}
        seen = set()
        older = []
        for entry in entries:
            key = (entry.kind, entry.timeframe)
            featured, featured_key = (
                (live, key) if entry.operational else (other, (*key, entry.label))
            )
            recent_key = (entry.kind, entry.label, entry.business_id)
            is_featured = featured_key not in featured
            # Check every displayed link, but skip the older archives beyond the recent list.
            if recent_key in seen or (not is_featured and len(older) == 20):
                continue
            if not _verified_link(entry, symbol_dir.name):
                continue
            seen.add(recent_key)
            if is_featured:
                featured[featured_key] = entry
            else:
                older.append(entry)
        sections = []
        if live:
            sections.append("<h2>最新实时报告</h2>" + _table(list(live.values()), symbol_dir))
        if other:
            sections.append("<h2>回放和演示</h2>" + _table(list(other.values()), symbol_dir))
        if older:
            sections.append(
                "<details><summary>最近归档</summary>" + _table(older, symbol_dir) + "</details>"
            )
        symbol = html.escape(symbol_dir.name)
        index = symbol_dir / "index.html"
        atomic_write(
            index,
            f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{symbol} · ElliottWaver 报告</title>
<style>
  body {{ font-family: Inter, system-ui, sans-serif; margin: 2rem auto; padding: 0 1rem; max-width: 70rem; }}
  main {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
  th, td {{ text-align: left; padding: .6rem; border-bottom: 1px solid #cbd5e1; }}
  summary {{ cursor: pointer; margin: 1rem 0; }}
</style>
<main><h1>{symbol} 报告</h1>{"".join(sections)}</main>
</html>""",
        )
    return index
