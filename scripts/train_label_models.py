"""Train per-template recognition probability models from blind labels."""

import argparse
import json
from pathlib import Path

from eliotbot.label_models import train_label_models


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    release = json.loads(args.release.read_text()) if args.release else None
    summary = train_label_models(args.cases, args.labels, args.output_dir, release=release)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
