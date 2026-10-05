# Corey Mathie, 2026
"""
Write demo/data/evals.json, the measured numbers the console's Evals and Overview
screens show in demo mode (GitHub Pages has no backend to run them).

Everything in the file comes from running the repo's own checks:

  call_evals          python -m evals.run's harness (31 scenarios, real Lambda handler code)
  simulated_callers   python -m evals.simulate's harness (14 personas, transcripts included)
  mutation_tests      the mutation tests in tests/test_evals.py and tests/test_simulate.py
  pytest              the whole suite's pass/fail/skip counts

    python scripts/export_console_data.py                 # run everything, write the file
    python scripts/export_console_data.py --check         # exit 1 if the committed evals/simulator results are stale
    python scripts/export_console_data.py --skip-pytest   # keep the committed pytest + mutation sections

--check re-runs the evals and the simulator (fast, deterministic) and compares
them with the committed file; CI runs it so the console can't show stale results.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "demo" / "data" / "evals.json"
MUTATION = re.compile(r"^test_(evals_catch|simulator_catches|simulator_measures)_(.+)$")
DETERMINISTIC = ("policy", "call_evals", "simulated_callers")


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def run_evals() -> dict:
    from evals import harness, simulate
    from src.safeguards.policy_config import load_policy

    policy = load_policy(harness.POLICY_PATH)
    scenarios = json.loads(harness.results_json(harness.run_all(policy=policy)))
    personas = json.loads(simulate.results_json(simulate.run_all(policy=policy)))
    return {
        "policy": {
            "source": "config/policy.yaml",
            "sha256": policy.sha256,
            "ref": policy.version_ref,
        },
        "call_evals": {"command": "python -m evals.run", **scenarios},
        "simulated_callers": {"command": "python -m evals.simulate", **personas},
    }


def _describe(name: str) -> tuple[str, str]:
    """(what was switched off, which harness caught it) from a mutation test's name."""
    m = MUTATION.match(name)
    kind, rest = m.group(1), m.group(2)
    target = "call evals" if kind == "evals_catch" else "simulated callers"
    return rest.replace("_", " "), target


def run_pytest() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if not xml.exists():
            raise SystemExit(f"pytest produced no report:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        tree = ET.parse(xml)
    cases = []
    for tc in tree.iter("testcase"):
        outcome = "passed"
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome = "failed"
            elif child.tag == "skipped":
                outcome = "skipped"
        cases.append({"name": tc.get("name", ""), "file": tc.get("classname", ""), "outcome": outcome})
    mutations = []
    for c in cases:
        base = c["name"].split("[", 1)[0]
        if MUTATION.match(base):
            control, target = _describe(base)
            mutations.append(
                {
                    "test": base,
                    "file": c["file"].replace(".", "/") + ".py",
                    "switched_off": control,
                    "caught_by": target,
                    "passed": c["outcome"] == "passed",
                }
            )
    counts = {k: sum(1 for c in cases if c["outcome"] == k) for k in ("passed", "failed", "skipped")}
    return {
        "pytest": {"command": "pytest -q", "total": len(cases), **counts},
        "mutation_tests": {
            "command": "pytest tests/test_evals.py tests/test_simulate.py (mutation tests)",
            "about": "Each test switches off one control (or weakens the policy file) and passes only if the "
            "evals or the simulator then fail.",
            "passed": sum(m["passed"] for m in mutations),
            "total": len(mutations),
            "tests": mutations,
        },
    }


def _comparable(data: dict) -> str:
    return json.dumps({k: data.get(k) for k in DETERMINISTIC}, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true", help="compare fresh eval/simulator results with the file")
    ap.add_argument("--skip-pytest", action="store_true", help="keep the committed pytest and mutation sections")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    fresh = run_evals()
    if args.check:
        committed = json.loads(args.out.read_text()) if args.out.exists() else {}
        if _comparable(committed) != _comparable(fresh):
            print(f"{args.out.relative_to(ROOT)} is stale: run python scripts/export_console_data.py", file=sys.stderr)
            return 1
        print(f"{args.out.relative_to(ROOT)} matches the current evals and simulator results")
        return 0

    previous = json.loads(args.out.read_text()) if args.out.exists() else {}
    tests = (
        {k: previous[k] for k in ("pytest", "mutation_tests") if k in previous} if args.skip_pytest else run_pytest()
    )
    data = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "version": _version(),
        "note": "Measured by scripts/export_console_data.py from this repo's own eval scripts and tests. "
        "Text-level only: no audio, no language model, outside services faked.",
        **fresh,
        **tests,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    ce, sc = data["call_evals"], data["simulated_callers"]["metrics"]
    mt, pt = data.get("mutation_tests", {}), data.get("pytest", {})
    print(
        f"wrote {args.out.relative_to(ROOT)}: call evals {ce['passed']}/{ce['total']}, simulated callers "
        f"{sc['expectations_met']}/{sc['personas']}, mutation tests {mt.get('passed')}/{mt.get('total')}, "
        f"pytest {pt.get('passed')} passed / {pt.get('failed')} failed / {pt.get('skipped')} skipped"
    )
    failed = ce["passed"] != ce["total"] or sc["expectations_met"] != sc["personas"] or pt.get("failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
