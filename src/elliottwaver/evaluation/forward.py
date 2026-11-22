"""Verified live report receipts and the precommitted portion of a holdout protocol."""

from pathlib import Path

from elliottwaver.errors import InputError
from elliottwaver.schemas.brief import Brief
from elliottwaver.visualization.brief_formatter import read_brief_report


def holdout_plan(protocol: dict) -> dict:
    """Freeze the cohort and rules before the identities of future snapshots are known."""
    return {
        **{key: value for key, value in protocol.items() if key not in {"samples", "data_status"}},
        "samples": [
            {key: value for key, value in sample.items() if key != "snapshot_id"}
            for sample in protocol["samples"]
        ],
    }


def live_reports(root: Path) -> dict[tuple[str, str, str], Brief]:
    receipts = {}
    for body in sorted(root.rglob("brief.json")):
        if any(part.startswith(".") for part in body.relative_to(root).parts[:-1]):
            continue
        brief, result = read_brief_report(body.parent)
        if brief.mode != "LIVE" or result.mode != "LIVE" or brief.retrospective:
            continue
        key = (brief.symbol, brief.timeframe, str(brief.market_asof))
        previous = receipts.get(key)
        if previous is not None and previous.brief_digest != brief.brief_digest:
            raise InputError(f"Multiple live decisions for {key}; select the issued reports")
        receipts[key] = brief
    if not receipts:
        raise InputError(f"No verified live decision reports in {root}")
    return receipts
