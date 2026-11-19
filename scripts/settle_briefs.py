"""Register and settle published briefs incrementally."""

import argparse
import json
from pathlib import Path

from eliotbot.config import load_settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.schemas.brief import Brief
from eliotbot.settlement import SettlementStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    settings = load_settings(args.config)
    root = args.cache_root or settings.cache.directory
    store = SettlementStore(root)
    snapshots = SnapshotStore(root)
    paths = [args.reports] if args.reports.is_file() else sorted(args.reports.rglob("brief.json"))
    records = []
    for brief_path in paths:
        try:
            brief = Brief.model_validate_json(brief_path.read_text())
            store.register_brief(brief, brief_path.parent)
            source = (
                snapshots.load(args.snapshot)
                if args.snapshot
                else snapshots.load(brief.source_snapshot_id)
            )
            store.settle_brief(brief, brief_path.parent, source)
            records.extend(store.rows(brief_digest=brief.brief_digest))
        except Exception as exc:  # noqa: BLE001
            records.append(
                {
                    "brief_path": str(brief_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    result = {"schema_version": "1.0", "records": records}
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
