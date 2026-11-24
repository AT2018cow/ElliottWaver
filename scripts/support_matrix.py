"""Write the asset/timeframe/event support matrix."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from elliottwaver.settlement import SUPPORT_TESTS, SupportTestReceipt, support_matrix
from elliottwaver.util import atomic_write, package_source_digest, support_test_inputs_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-summary", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--test-receipts", type=Path)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    exit_code = 0
    if args.run_tests:
        if args.test_receipts is None:
            parser.error("--run-tests requires --test-receipts")
        root = Path(__file__).resolve().parents[1]
        source = package_source_digest()
        inputs = support_test_inputs_digest(root)
        if inputs is None:
            parser.error("test and configuration sources are required")
        for name in SUPPORT_TESTS:
            command = (sys.executable, "-m", "pytest", name, "-q")
            started = datetime.now(UTC)
            result = subprocess.run(command, stdout=sys.stderr, check=False, cwd=root)
            if package_source_digest() != source or support_test_inputs_digest(root) != inputs:
                raise RuntimeError("Package source changed during support matrix tests")
            receipt = SupportTestReceipt(
                source_sha256=source,
                test_inputs_sha256=inputs,
                command=command,
                started_at=started,
                completed_at=datetime.now(UTC),
                exit_code=result.returncode,
            )
            atomic_write(
                args.test_receipts / (Path(name).stem + ".json"),
                receipt.model_dump_json() + "\n",
            )
            if result.returncode:
                exit_code = 1
    summary = json.loads(args.model_summary.read_text()) if args.model_summary else None
    matrix = support_matrix(summary, test_receipts=args.test_receipts)
    if args.output:
        args.output.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(matrix, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
