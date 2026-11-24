"""Verified live report receipts and the precommitted portion of a holdout protocol."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from elliottwaver.errors import InputError
from elliottwaver.schemas.brief import Brief
from elliottwaver.visualization.brief_formatter import read_brief_report, read_issuance


@dataclass(frozen=True)
class LiveReport:
    brief: Brief
    readable_at: datetime


def holdout_plan(protocol: dict) -> dict:
    """Freeze the cohort and rules before the identities of future snapshots are known."""
    return {
        **{key: value for key, value in protocol.items() if key not in {"samples", "data_status"}},
        "samples": [
            {key: value for key, value in sample.items() if key != "snapshot_id"}
            for sample in protocol["samples"]
        ],
    }


def live_receipt_key(
    symbol: str, timeframe: str, session: date | str, bar_end: datetime | str | None = None
) -> tuple[str, str, str]:
    if timeframe.endswith("m"):
        end = datetime.fromisoformat(bar_end) if isinstance(bar_end, str) else bar_end
        if end is None or end.utcoffset() is None:
            raise InputError("Intraday live receipt requires a timezone-aware bar end")
        return symbol, timeframe, end.astimezone(UTC).isoformat()
    return symbol, timeframe, str(session)


def live_reports(root: Path) -> dict[tuple[str, str, str], LiveReport]:
    receipts = {}
    for body in sorted(root.rglob("brief.json")):
        if any(part.startswith(".") for part in body.relative_to(root).parts[:-1]):
            continue
        brief, result = read_brief_report(body.parent)
        if brief.mode != "LIVE" or result.mode != "LIVE" or brief.retrospective:
            continue
        readable_at = read_issuance(body.parent, brief)
        if readable_at is None:
            continue
        key = live_receipt_key(
            brief.symbol, brief.timeframe, brief.market_asof, brief.last_bar_end_at
        )
        previous = receipts.get(key)
        if previous is not None and previous.brief.brief_digest != brief.brief_digest:
            raise InputError(f"Multiple live decisions for {key}; select the issued reports")
        if previous is None or readable_at < previous.readable_at:
            receipts[key] = LiveReport(brief, readable_at)
    if not receipts:
        raise InputError(f"No verified live decision reports in {root}")
    return receipts
