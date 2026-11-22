"""Write the asset/timeframe/event support matrix."""

import argparse
import json
from pathlib import Path

from elliottwaver.settlement import support_matrix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-summary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = json.loads(args.model_summary.read_text()) if args.model_summary else None
    matrix = support_matrix(summary)
    if args.output:
        args.output.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(matrix, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
