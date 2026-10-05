# Corey Mathie, 2026
"""
Run the call scenarios and print a scorecard.

    python -m evals.run                         # all scenarios; exit code 1 if any fail
    python -m evals.run --only payment-burst    # one or more by id
    python -m evals.run --markdown report.md --json report.json
    python -m evals.run --policy proposed-policy.yaml   # test a policy change against every scenario

In GitHub Actions the scorecard is also written to the job summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.safeguards.policy_config import PolicyConfigError, load_policy

from .harness import POLICY_PATH, SCENARIOS_PATH, load_scenarios, results_json, run_all, scorecard_markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Voice agent call evals")
    ap.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--markdown", type=Path, help="write the scorecard here")
    ap.add_argument("--json", type=Path, help="write full results here")
    ap.add_argument("--policy", type=Path, default=POLICY_PATH, help="policy file to evaluate (config/policy.yaml)")
    args = ap.parse_args(argv)

    try:
        policy = load_policy(args.policy)
    except PolicyConfigError as e:
        print(f"invalid policy: {e}", file=sys.stderr)
        return 2
    results = run_all(load_scenarios(args.scenarios), only=args.only, policy=policy)
    if not results:
        print("no scenarios matched", file=sys.stderr)
        return 2
    try:
        shown = Path(policy.source).resolve().relative_to(Path.cwd())
    except ValueError:
        shown = Path(policy.source).name
    card = scorecard_markdown(results) + f"\nPolicy: `{shown}` (sha256 `{policy.version_ref}`)\n"
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
