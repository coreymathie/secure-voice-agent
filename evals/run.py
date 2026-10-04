# Corey Mathie, 2026
"""
Run the call scenarios and print a scorecard.

    python -m evals.run                         # all scenarios; exit code 1 if any fail
    python -m evals.run --only payment-burst    # one or more by id
    python -m evals.run --markdown report.md --json report.json

In GitHub Actions the scorecard is also written to the job summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .harness import SCENARIOS_PATH, load_scenarios, results_json, run_all, scorecard_markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Voice agent call evals")
    ap.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--markdown", type=Path, help="write the scorecard here")
    ap.add_argument("--json", type=Path, help="write full results here")
    args = ap.parse_args(argv)

    results = run_all(load_scenarios(args.scenarios), only=args.only)
    if not results:
        print("no scenarios matched", file=sys.stderr)
        return 2
    card = scorecard_markdown(results)
    print(card)
    if args.markdown:
        args.markdown.write_text(card)
    if args.json:
        args.json.write_text(results_json(results))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(card)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
