"""Run the offline decision/outcome evaluation protocol."""

import argparse
import json
from pathlib import Path

from elliottwaver.evaluation.decisions import evaluate_protocol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-registry", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--model-approved-at")
    parser.add_argument("--live-reports", type=Path)
    args = parser.parse_args()
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
