"""Run the offline decision/outcome evaluation protocol."""

import argparse
import json
from pathlib import Path

from elliottwaver.evaluation.decisions import evaluate_protocol
from elliottwaver.evaluation.index_research import (
    evaluate_index_trading,
    seal_trading_protocol,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-registry", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--model-approved-at")
    parser.add_argument("--live-reports", type=Path)
    parser.add_argument("--index-reports", type=Path)
    parser.add_argument("--snapshot-store", type=Path)
    parser.add_argument("--tool-archive", type=Path)
    parser.add_argument("--execution-seal", type=Path)
    parser.add_argument("--execution-split", choices=("DEVELOPMENT", "CALIBRATION", "HOLDOUT"))
    parser.add_argument("--seal-forward-execution", action="store_true")
    args = parser.parse_args()
    if json.loads(args.protocol.read_text()).get("kind") == "SINGLE_INDEX_EXECUTION":
        if args.model_registry or args.model or args.model_approved_at:
            parser.error("execution protocols use the archived reports' original decisions")
        if args.seal_forward_execution:
            summary = seal_trading_protocol(args.protocol, args.output_dir)
        else:
            reports = args.index_reports or args.live_reports
            if reports is None:
                parser.error("execution evaluation requires --index-reports or --live-reports")
            if args.index_reports and args.live_reports:
                parser.error("please select a single evidence directory")
            summary = evaluate_index_trading(
                args.protocol,
                args.output_dir,
                report_directory=reports,
                snapshot_directory=args.snapshot_store,
                tool_archive_path=args.tool_archive,
                seal_path=args.execution_seal,
                only_split=args.execution_split,
            )
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return 0
    if any(
        (
            args.index_reports,
            args.snapshot_store,
            args.tool_archive,
            args.execution_seal,
            args.execution_split,
            args.seal_forward_execution,
        )
    ):
        parser.error("execution flags require a SINGLE_INDEX_EXECUTION protocol")
    summary = evaluate_protocol(
        args.protocol,
        args.output_dir,
        model_registry=args.model_registry,
        model=args.model,
        model_approved_at=args.model_approved_at,
        issued_reports=args.live_reports,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
