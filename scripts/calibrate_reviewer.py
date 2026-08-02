#!/usr/bin/env python3
"""CLI runner for the review-calibration metrics engine (P19/P20).

Usage:
    calibrate_reviewer.py --review-cmd <cmd> --corpus <dir> [--output json|<path>]

``--review-cmd`` receives each fixture's diff on stdin and must print JSON
findings to stdout (either a bare list or ``{"findings": [...]}"``).
``--output`` selects where the resulting metrics JSON goes: the default,
``json``, prints it to stdout; any other value is treated as a file path to
write it to instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adversarial_common import calibration  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="calibrate_reviewer.py")
    parser.add_argument(
        "--review-cmd", required=True,
        help="Reviewer command; receives the diff on stdin, prints JSON findings on stdout.",
    )
    parser.add_argument(
        "--corpus", required=True,
        help="Path to the calibration corpus directory (P18 layout).",
    )
    parser.add_argument(
        "--output", default="json",
        help='"json" (default) prints metrics to stdout; any other value is a file path.',
    )
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus)
    if not corpus_dir.is_dir():
        print(f"calibrate_reviewer.py: corpus directory not found: {corpus_dir}", file=sys.stderr)
        return 1

    try:
        metrics = calibration.run_calibration(corpus_dir, args.review_cmd)
    except Exception as exc:
        print(f"calibrate_reviewer.py: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(metrics, indent=2, sort_keys=True)
    if args.output == "json":
        print(payload)
    else:
        Path(args.output).write_text(payload + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
