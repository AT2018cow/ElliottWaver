"""Run the offline decision/outcome evaluation protocol."""

import argparse
import json
from pathlib import Path

from eliotbot.evaluation.decisions import evaluate_protocol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-registry", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--model-approved-at")
    args = parser.parse_args()
    summary = evaluate_protocol(
        args.protocol,
        args.output_dir,
        model_registry=args.model_registry,
        model=args.model,
        model_approved_at=args.model_approved_at,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
