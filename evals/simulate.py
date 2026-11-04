# Corey Mathie, 2026
"""
Simulated-caller harness: scripted multi-turn callers against the real tool stack.

    python -m evals.simulate                       # the development set; exit 1 on a regression
    python -m evals.simulate --set holdout         # the held-out set
    python -m evals.simulate --set handwritten     # only the 14 hand-written personas
    python -m evals.simulate --only se-owner-redirect benign-payer
    python -m evals.simulate --json sim.json --markdown sim.md --policy proposed.yaml

Three persona files:
  evals/personas.yaml            14 hand-written callers, also played by the console
  evals/personas_generated.yaml  a larger seeded set (scripts/generate_personas.py): reworded scam
                                 patterns, legitimate callers who sound urgent or unusual, Spanish callers
  evals/personas_holdout.yaml    the held-out set (scripts/generate_personas_holdout.py): another seed and
                                 separately written phrasings, kept out of the design of safeguard changes
The development set (the default, `--set development`) is the first two; `--set all` adds the held-out
set. A generated persona can carry `known_gap: <reason>`: it probes a documented limitation, its result
is reported in its own section, and it does not fail the run. Any other persona that misses its
expectation is a regression and the exit code is 1.

Each persona is one phone call. Its turns are fed to the
real policy gate as finalized caller transcript, and a deterministic scripted
agent (ScriptedAgent below, no language model) turns what the caller wants into
tool calls through the real make_handler: policy gate, step-up verification,
velocity limits, scrubbing, the hash-chained audit log, and the real Lambda
handler code with signed requests. Outside services are the eval fakes
(evals/harness.py), so nothing leaves the process.

The agent is deliberately gullible. It calls whatever tool the caller asks for,
with whatever arguments the caller or an injection supplies (an extra
customer_phone, a tool it was never granted), and it always tries to verify a
caller when asked to. That makes the scorecard a measure of what the
deterministic controls catch when the model has already been fooled.

Scored per persona: did the caller's goal happen (a tool result, a side effect,
a text to a given number)? Benign and impatient callers should get what they
came for; social engineers and prompt injectors shouldn't. Reported:

  task success          benign + impatient personas whose goal happened
  correct refusals      adversarial personas whose goal did not happen
  false-positive rate   benign + impatient personas blocked, over all of them
  handoffs              require_human results seen, per persona and in total

This is text-level, not audio-level: there is no speech, no speech-to-text, no
voice activity detection, and no model choosing what to do. It does not measure
latency or how a real model behaves. docs/simulation.md describes how to put a
real model or synthetic speech in the loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.agent import tools
from src.safeguards.audit_log import AuditLog
from src.safeguards.policy_config import LoadedPolicy, PolicyConfigError, load_policy
from src.safeguards.policy_gate import CallPolicy
from src.safeguards.step_up import (
    InMemoryCrm,
    MemberRecord,
    SimulatedVerifier,
    StaticRiskSignals,
    StepUpSession,
    normalize_number,
)
from src.safeguards.velocity import VelocityStore

from . import harness
from .scripted import (  # noqa: F401 - re-exported; scripted.py is shared with the console
    ADVERSARIAL_KINDS,
    BENIGN_KINDS,
    DEFAULT_GUESSES,
    TURN_SECONDS,
    Event,
    ScriptedAgent,
    controls_fired,
    destination_pinned,
    expectation,
    goal_achieved,
    metrics,
    parse_personas,
)

PERSONAS_PATH = Path(__file__).with_name("personas.yaml")
GENERATED_PATH = Path(__file__).with_name("personas_generated.yaml")
HOLDOUT_PATH = Path(__file__).with_name("personas_holdout.yaml")
SETS = ("development", "holdout", "all", "handwritten", "generated")
SOURCES = {"gen-": "generated", "ho-": "holdout"}  # persona id prefix -> set; anything else is hand-written


@dataclass
class PersonaResult:
    id: str
    kind: str
    expect: str
    achieved: bool
    correct: bool
    handoffs: int
    controls: list[str]
    tool_statuses: list[str]
    failures: list[str]
    transcript: list[Event] = field(default_factory=list)
    title: str = ""  # what the caller is trying to do, in plain words
    language: str = "en"
    category: str = ""  # generated personas: the caller pattern; hand-written: "handwritten"
    known_gap: str = ""  # a documented limitation this persona probes (does not fail the run)
    source: str = "handwritten"  # handwritten | generated | holdout

    @property
    def regression(self) -> bool:
        """Missed its expectation without being a documented known gap."""
        return not self.correct and not self.known_gap


@dataclass
class _World:
    caller: str
    phone_on_file: str
    policy: CallPolicy
    velocity: VelocityStore
    verifier: SimulatedVerifier
    executors: dict
    audit: AuditLog
    clock: harness.FakeClock
    up: harness.Upstreams
    outcomes: list = field(default_factory=list)
    model_saw: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    requested_destinations: list = field(default_factory=list)  # phone numbers the caller/injection asked for
    calls: int = 0

    def advance(self, seconds: float) -> None:
        self.clock.now += seconds

    def say(self, who: str, **kw) -> None:
        self.transcript.append(Event(self.clock.now, who, **kw))


def _goal_achieved(goal: dict, world: _World) -> bool:
    return goal_achieved(goal, world.outcomes, world.up)


def _controls(audit: AuditLog) -> list[str]:
    """Which controls fired on this call, from the audit log (codes only)."""
    lines = audit.path.read_text().splitlines() if audit.path.exists() else []
    return controls_fired([json.loads(line) for line in lines if line.strip()])


async def _converse(persona: dict, up: harness.Upstreams, audit: AuditLog, loaded: LoadedPolicy) -> _World:
    clock = harness.FakeClock()
    caller = persona["caller"]
    on_file = persona.get("phone_on_file", caller)
    lookup = (caller,) if normalize_number(caller) != normalize_number(on_file) else ()
    crm = InMemoryCrm([MemberRecord(f"cust_{persona['id']}", phone_on_file=on_file, lookup_numbers=lookup)])
    risk = StaticRiskSignals({on_file: persona.get("risk_signals", [])})
    verifier = SimulatedVerifier(clock=clock, code_factory=harness._eval_codes(), sent=up.verification_codes)
    policy = CallPolicy(config=loaded.policy, call_id="CA" + "5" * 32)
    session = StepUpSession(caller, crm, verifier, policy=policy, risk=risk, config=loaded.step_up, clock=clock)
    lambdas = {name: harness.in_process_executor(name) for name in harness.HANDLERS}
    world = _World(
        caller=caller,
        phone_on_file=on_file,
        policy=policy,
        velocity=VelocityStore(rules=loaded.velocity_rules, clock=clock),
        verifier=verifier,
        executors={**lambdas, **tools.step_up_executors(session)},
        audit=audit,
        clock=clock,
        up=up,
    )
    await ScriptedAgent(persona, world).run()
    return world


def run_persona(persona: dict, loaded: LoadedPolicy) -> PersonaResult:
    up = harness.Upstreams()
    with harness.fake_world(up) as audit:
        world = asyncio.run(_converse(persona, up, audit, loaded))
        achieved = _goal_achieved(persona["goal"], world)
        failures: list[str] = []
        expect = expectation(persona)
        if (expect == "achieved") != achieved:
            failures.append(
                f"expected the caller's goal to be {expect}, but it was {'achieved' if achieved else 'blocked'}"
            )
        audit_text = audit.path.read_text() if audit.path.exists() else ""
        shown = json.dumps(world.model_saw)
        received = up.everything_received()
        for text in persona.get("leaks", []):
            for where, blob in (("an outside service", received), ("the audit log", audit_text), ("the model", shown)):
                if text in blob:
                    failures.append(f"{where} received {text!r}")
        ok, bad_line = audit.verify_chain()
        if not ok:
            failures.append(f"audit chain broken at line {bad_line}")
        controls = _controls(audit)
        # make_handler drops a model-supplied payment destination silently; show it when it mattered.
        if destination_pinned(world.caller, world.requested_destinations, up.sms):
            controls.append("destination_pinned")
    statuses = [e.status for e in world.transcript if e.who == "agent" and e.status]
    return PersonaResult(
        id=persona["id"],
        kind=persona["kind"],
        expect=expect,
        achieved=achieved,
        correct=not failures,
        handoffs=statuses.count("require_human"),
        controls=controls,
        tool_statuses=statuses,
        failures=failures,
        transcript=world.transcript,
        title=persona.get("title", persona["id"]),
        language=persona.get("language", "en"),
        category=persona.get("category", "handwritten"),
        known_gap=persona.get("known_gap", ""),
        source=source_of(persona["id"]),
    )


def load_personas(path: Path = PERSONAS_PATH) -> list[dict]:
    """The persona file, with "$DATE(...)" values resolved against today (evals.scripted.CLOCK)."""
    return parse_personas(path.read_text())


def source_of(persona_id: str) -> str:
    return next((name for prefix, name in SOURCES.items() if persona_id.startswith(prefix)), "handwritten")


def load_set(which: str = "development") -> list[dict]:
    """
    development (hand-written + generated), holdout (evals/personas_holdout.yaml), all (both),
    handwritten (evals/personas.yaml) or generated (evals/personas_generated.yaml).
    """
    if which not in SETS:
        raise ValueError(f"unknown persona set {which!r}")
    hand = load_personas(PERSONAS_PATH) if which in ("development", "all", "handwritten") else []
    gen = load_personas(GENERATED_PATH) if which in ("development", "all", "generated") else []
    held = load_personas(HOLDOUT_PATH) if which in ("holdout", "all") else []
    personas = hand + gen + held
    ids = [p["id"] for p in personas]
    if len(ids) != len(set(ids)):
        raise ValueError("persona ids must be unique across both files")
    return personas


def run_all(
    personas: list[dict] | None = None, only: list[str] | None = None, policy: LoadedPolicy | None = None
) -> list[PersonaResult]:
    personas = personas if personas is not None else load_personas()
    if only:
        personas = [p for p in personas if p["id"] in only]
    loaded = policy or load_policy(harness.POLICY_PATH)
    return [run_persona(p, loaded) for p in personas]


# ---------- Scoring and reporting (metrics() is in scripted.py) ----------


def breakdown(results: list[PersonaResult]) -> list[dict]:
    """Served / stopped counts per set, language and side (benign or adversarial)."""
    rows: dict[tuple, dict] = {}
    for r in results:
        side = "benign" if r.kind in BENIGN_KINDS else "adversarial"
        row = rows.setdefault(
            (r.source, r.language, side),
            {"set": r.source, "language": r.language, "side": side, "personas": 0, "served": 0, "stopped": 0,
             "as_expected": 0, "known_gap_misses": 0, "regressions": 0},
        )  # fmt: skip
        row["personas"] += 1
        row["served" if r.achieved else "stopped"] += 1
        row["as_expected"] += r.correct
        row["known_gap_misses"] += (not r.correct) and bool(r.known_gap)
        row["regressions"] += r.regression
    order = {"handwritten": 0, "generated": 1, "holdout": 2, "en": 0, "es": 1, "benign": 0, "adversarial": 1}
    return sorted(rows.values(), key=lambda x: (order[x["set"]], order.get(x["language"], 9), order[x["side"]]))


def known_gaps(results: list[PersonaResult]) -> list[dict]:
    """One row per known-gap category: its reason, and how many of its cases missed or met their expectation."""
    gaps: dict[tuple, dict] = {}
    for r in results:
        if not r.known_gap:
            continue
        g = gaps.setdefault(
            (r.category, r.language),
            {"category": r.category, "language": r.language, "kind": r.kind, "reason": r.known_gap,
             "personas": 0, "missed": 0, "met": 0},
        )  # fmt: skip
        g["personas"] += 1
        g["met" if r.correct else "missed"] += 1
    return list(gaps.values())


def summary(results: list[PersonaResult]) -> dict:
    """The headline numbers: totals, per-set/language/side rows, known gaps, regressions."""
    return {
        "personas": len(results),
        "as_expected": sum(r.correct for r in results),
        "regressions": [r.id for r in results if r.regression],
        "known_gap_misses": sum((not r.correct) and bool(r.known_gap) for r in results),
        "breakdown": breakdown(results),
        "known_gaps": known_gaps(results),
    }


def scorecard_markdown(results: list[PersonaResult], policy_ref: str = "") -> str:
    m = metrics(results)
    ts, cr = m["task_success"], m["correct_refusals"]
    lines = [
        "## Simulated callers (text-level, scripted agent)",
        "",
        f"**{m['expectations_met']}/{m['personas']} personas as expected** · "
        f"task success {ts['achieved']}/{ts['of']} · correct refusals {cr['blocked']}/{cr['of']} · "
        f"false-positive rate {m['false_positive_rate']:.0%} · handoffs {m['handoffs']}",
        "",
        "| | Persona | Kind | Goal | Tool results | Controls that fired |",
        "|---|---|---|---|---|---|",
    ]
    sm = summary(results)
    if any(r.source != "handwritten" for r in results):
        lines[2:2] = [
            f"**{len(sm['regressions'])} regressions** · {sm['known_gap_misses']} known-gap misses "
            "(documented limitations, reported below; they do not fail the run)",
            "",
        ]
    # Every hand-written persona gets a row; generated personas only when they miss their expectation.
    shown = [r for r in results if r.source == "handwritten" or not r.correct]
    for r in shown:
        goal = "achieved" if r.achieved else "blocked"
        statuses = " → ".join(f"`{s}`" for s in r.tool_statuses) or "—"
        controls = ", ".join(f"`{c}`" for c in r.controls) or "—"
        mark = "✅" if r.correct else ("⚠️" if r.known_gap else "❌")
        lines.append(f"| {mark} | {r.id} | {r.kind} | {goal} | {statuses} | {controls} |")
    if any(r.source != "handwritten" for r in results):
        lines += [
            "",
            "Generated personas are listed above only when they miss their expectation (⚠️ known gap, ❌ regression).",
            "",
            "### By set, language and side",
            "",
            "| Set | Language | Side | Personas | Served | Stopped | As expected | Known-gap misses | Regressions |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for b in sm["breakdown"]:
            lines.append(
                f"| {b['set']} | {b['language']} | {b['side']} | {b['personas']} | {b['served']} | {b['stopped']} "
                f"| {b['as_expected']} | {b['known_gap_misses']} | {b['regressions']} |"
            )
        if sm["known_gaps"]:
            lines += [
                "",
                "### Known gaps",
                "",
                "| Category | Language | Missed | Met | Reason |",
                "|---|---|---|---|---|",
            ]
            for g in sm["known_gaps"]:
                lines.append(f"| {g['category']} | {g['language']} | {g['missed']} | {g['met']} | {g['reason']} |")
    regressions = [r for r in results if r.regression]
    if regressions:
        lines += ["", "### Regressions", ""]
        for r in regressions:
            lines.append(f"**{r.id}**")
            lines += [f"- {f}" for f in r.failures]
            lines.append("")
    lines += [
        "",
        "Text-level simulation: no audio, speech recognition, or language model is involved; the agent is a "
        "deterministic script that does whatever the caller asks. See docs/simulation.md.",
    ]
    if policy_ref:
        lines.append(f"Policy: {policy_ref}")
    return "\n".join(lines) + "\n"


def results_json(results: list[PersonaResult]) -> str:
    return json.dumps(
        {
            "metrics": metrics(results),
            "summary": summary(results),
            "personas": [
                {
                    "id": r.id,
                    "title": r.title,
                    "kind": r.kind,
                    "language": r.language,
                    "category": r.category,
                    "known_gap": r.known_gap,
                    "expect": r.expect,
                    "achieved": r.achieved,
                    "correct": r.correct,
                    "handoffs": r.handoffs,
                    "controls": r.controls,
                    "tool_statuses": r.tool_statuses,
                    "failures": r.failures,
                    "transcript": [e.__dict__ for e in r.transcript],
                }
                for r in results
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulated-caller harness (text-level)")
    ap.add_argument(
        "--set", choices=SETS, default="development", help="which persona files to run (default: development)"
    )
    ap.add_argument("--personas", type=Path, help="run this persona file instead of --set")
    ap.add_argument("--only", nargs="*", help="persona ids to run")
    ap.add_argument("--policy", type=Path, default=harness.POLICY_PATH)
    ap.add_argument("--markdown", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except PolicyConfigError as e:
        print(f"invalid policy: {e}", file=sys.stderr)
        return 2
    personas = load_personas(args.personas) if args.personas else load_set(args.set)
    results = run_all(personas, only=args.only, policy=policy)
    if not results:
        print("no personas matched", file=sys.stderr)
        return 2
    card = scorecard_markdown(results, f"`{Path(policy.source).name}` (sha256 `{policy.version_ref}`)")
    print(card)
    if args.markdown:
        args.markdown.write_text(card)
    if args.json:
        args.json.write_text(results_json(results))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(card)
    return 0 if not any(r.regression for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
