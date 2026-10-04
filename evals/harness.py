# Corey Mathie, 2026
"""
Call-evaluation harness for the agent's tool layer.

Each scenario replays the tool calls a model would make during one phone call
(with timing) through the real stack:

    make_handler -> velocity limits -> PII scrubbing -> hash-chained audit log
      -> real Lambda handler code (validation, DynamoDB-style idempotency)
        -> fake Stripe / Twilio SMS / Google Calendar / Zendesk / CRM

Only the outside services are fake, and each fake enforces the real service's
rules where they matter (Google Calendar rejects a dateTime with no time zone).
A fake clock drives the velocity windows, so an hour-long call runs instantly.

The scenarios are about what can go wrong on a live line: a caller (or a
confused model) asking for repeated charges, a replayed request, a provider
outage mid-call, a card number spoken into a ticket, a prompt-injected attempt
to text a payment link to someone else.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import yaml

from src.agent import tools
from src.handlers import _common, book_meeting, create_ticket, log_lead, take_payment
from src.safeguards.audit_log import AuditLog
from src.safeguards.velocity import VelocityStore

SCENARIOS_PATH = Path(__file__).with_name("scenarios.yaml")
HANDLERS = {
    "book_meeting": book_meeting,
    "take_payment": take_payment,
    "create_ticket": create_ticket,
    "log_lead": log_lead,
}
FAKE_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_eval",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    "GOOGLE_CALENDAR_ID": "primary",
    "BUSINESS_TIMEZONE": "America/New_York",
    "ZENDESK_SUBDOMAIN": "eval",
    "ZENDESK_EMAIL": "ops@example.com",
    "ZENDESK_API_TOKEN": "eval",
    "GOHIGHLEVEL_API_KEY": "eval",
    "GOHIGHLEVEL_LOCATION_ID": "loc_eval",
    "AUDIT_SALT": "eval-salt",
}


# ---------- Fakes ----------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ConditionalCheckFailedException(Exception):  # same name boto3 raises
    pass


class MemoryTable:
    """DynamoDB table stand-in supporting the conditional put the idempotency layer uses."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None):
        if ConditionExpression and Item["key"] in self.items:
            raise ConditionalCheckFailedException("The conditional request failed")
        self.items[Item["key"]] = Item

    def delete_item(self, Key):
        self.items.pop(Key["key"], None)


class ServiceDown(Exception):
    pass


@dataclass
class Upstreams:
    """Everything the outside world received, plus which services are down right now."""

    down: set[str] = field(default_factory=set)
    payment_links: list[dict] = field(default_factory=list)
    sms: list[dict] = field(default_factory=list)
    calendar_events: list[dict] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    crm_contacts: list[dict] = field(default_factory=list)

    def check(self, service: str) -> None:
        if service in self.down:
            raise ServiceDown(f"{service} unavailable: connection reset by api.{service}.com (internal trace id 7f3a)")

    def everything_received(self) -> str:
        return json.dumps(
            [self.payment_links, self.sms, self.calendar_events, self.tickets, self.crm_contacts], default=str
        )

    # Stripe
    def stripe_module(self) -> SimpleNamespace:
        up = self

        def create(kind):
            def _create(**kw):
                up.check("stripe")
                obj = SimpleNamespace(id=f"{kind}_{len(up.payment_links)}", **kw)
                if kind == "plink":
                    obj.url = f"https://buy.stripe.com/test_{len(up.payment_links):04d}"
                    up.payment_links.append(kw)
                return obj

            return _create

        return SimpleNamespace(
            api_key=None,
            Product=SimpleNamespace(create=create("prod")),
            Price=SimpleNamespace(create=create("price")),
            PaymentLink=SimpleNamespace(create=create("plink")),
        )

    # Twilio SMS
    def send_sms(self, to: str, text: str) -> bool:
        if not to or to == "unknown":
            return False
        self.check("sms")
        self.sms.append({"to": to, "text": text})
        return True

    # Google Calendar
    def calendar(self) -> SimpleNamespace:
        up = self

        def insert(calendarId, body, sendUpdates=None):
            def execute():
                up.check("calendar")
                for edge in ("start", "end"):
                    dt = body[edge]["dateTime"]
                    has_offset = dt.endswith("Z") or "+" in dt[10:] or "-" in dt[10:]
                    if not has_offset and not body[edge].get("timeZone"):
                        raise RuntimeError(f"HttpError 400: Missing time zone definition for {edge} time.")
                up.calendar_events.append(body)
                return {"id": f"evt_{len(up.calendar_events)}"}

            return SimpleNamespace(execute=execute)

        return SimpleNamespace(events=lambda: SimpleNamespace(insert=insert))

    # Zendesk
    def zendesk(self) -> SimpleNamespace:
        up = self

        def create(ticket):
            up.check("zendesk")
            up.tickets.append({"subject": ticket.subject, "description": ticket.description})
            return SimpleNamespace(ticket=SimpleNamespace(id=1000 + len(up.tickets)))

        return SimpleNamespace(tickets=SimpleNamespace(create=create))

    # CRM (GoHighLevel)
    def crm_httpx(self) -> SimpleNamespace:
        up = self

        def route(request: httpx.Request) -> httpx.Response:
            if "crm" in up.down:
                return httpx.Response(503, json={"message": "service unavailable"})
            body = json.loads(request.content or b"{}")
            if request.url.path == "/contacts/upsert":
                up.crm_contacts.append(body)
                return httpx.Response(200, json={"contact": {"id": f"ct_{len(up.crm_contacts)}"}})
            return httpx.Response(404)

        transport = httpx.MockTransport(route)
        return SimpleNamespace(Client=lambda **kw: httpx.Client(transport=transport, **kw), HTTPError=httpx.HTTPError)


def in_process_executor(tool: str):
    """Call the Lambda handler directly, mapping its HTTP status the way the real executor does."""
    module = HANDLERS[tool]

    async def run(args: dict, idempotency_key: str) -> dict:
        event = {"body": json.dumps(args), "headers": {"Idempotency-Key": idempotency_key}}
        try:
            resp = module.handler(event, None)
        except Exception as e:
            raise RuntimeError(f"502 from API Gateway: {tool} Lambda raised {e}") from e
        body = json.loads(resp["body"])
        if 400 <= resp["statusCode"] < 500:
            raise tools.ToolRejected(body.get("error", "rejected"))
        if resp["statusCode"] >= 500:
            raise RuntimeError(f"{resp['statusCode']} from {tool}")
        return body

    return run


# ---------- Running a scenario ----------


@dataclass
class StepResult:
    at: float
    tool: str
    expected: str
    got: str
    reason: str | None
    ok: bool


@dataclass
class ScenarioResult:
    id: str
    title: str
    risk: str
    passed: bool
    steps: list[StepResult]
    failures: list[str]


def _expectation(spec: Any) -> dict:
    return {"status": spec} if isinstance(spec, str) else dict(spec)


async def _run_steps(scenario: dict, up: Upstreams, audit: AuditLog) -> tuple[list[StepResult], list[str], list]:
    clock = FakeClock()
    velocity = VelocityStore(clock=clock)
    executors = {name: in_process_executor(name) for name in HANDLERS}
    steps: list[StepResult] = []
    failures: list[str] = []
    llm_saw: list = []

    for i, step in enumerate(scenario["steps"], start=1):
        clock.now = float(step.get("at", 0))
        up.down = set(step.get("upstream_down", []))
        caller = step.get("caller", scenario.get("caller", "+15555550100"))
        handler = tools.make_handler(step["tool"], caller, executors, velocity=velocity, audit=audit)

        results: list[dict] = []

        async def result_callback(result, **_):
            results.append(result)  # noqa: B023 - consumed before the next iteration

        params = SimpleNamespace(
            arguments=step.get("args", {}),
            tool_call_id=step.get("call_id", f"call_{i}"),
            result_callback=result_callback,
        )
        await handler(params)
        got = results[0] if results else {"status": "<no result>"}
        llm_saw.append(got)

        want = _expectation(step["expect"])
        problems = []
        if got.get("status") != want["status"]:
            problems.append(f"expected {want['status']!r}, got {got.get('status')!r}")
        if "reason_contains" in want and want["reason_contains"].lower() not in str(got.get("reason", "")).lower():
            problems.append(f"reason should mention {want['reason_contains']!r}: {got.get('reason')!r}")
        for key in want.get("absent_keys", []):
            if key in got:
                problems.append(f"result should not include {key!r} (the agent would read it aloud)")
        for p in problems:
            failures.append(f"step {i} ({step['tool']} at {clock.now:g}s): {p}")
        steps.append(
            StepResult(clock.now, step["tool"], want["status"], str(got.get("status")), got.get("reason"), not problems)
        )
    return steps, failures, llm_saw


def _check(scenario: dict, up: Upstreams, audit: AuditLog, llm_saw: list) -> list[str]:
    failures: list[str] = []
    checks = scenario.get("checks", {})
    audit_text = audit.path.read_text() if audit.path.exists() else ""
    events = [json.loads(line)["event"] for line in audit_text.splitlines() if line.strip()]

    for name, want in checks.get("side_effects", {}).items():
        got = len(getattr(up, name))
        if got != want:
            failures.append(f"{name}: expected {want}, got {got}")
    if "sms_to" in checks:
        wrong = [m["to"] for m in up.sms if m["to"] != checks["sms_to"]]
        if wrong:
            failures.append(f"SMS went to {wrong}, not the caller {checks['sms_to']}")
    for ev in checks.get("audit_has", []):
        if ev not in events:
            failures.append(f"audit log is missing a {ev!r} event")
    for text in checks.get("audit_lacks", []):
        if text in audit_text:
            failures.append(f"audit log contains {text!r}")
    received = up.everything_received()
    for text in checks.get("upstream_lacks", []):
        if text in received:
            failures.append(f"an outside service received {text!r}")
    said = json.dumps(llm_saw)
    for text in checks.get("llm_lacks", []):
        if text.lower() in said.lower():
            failures.append(f"the model was shown {text!r}")
    ok, bad_line = audit.verify_chain()
    if not ok:
        failures.append(f"audit chain broken at line {bad_line}")
    return failures


def run_scenario(scenario: dict) -> ScenarioResult:
    up = Upstreams()
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, FAKE_ENV))
        stack.enter_context(patch.dict(sys.modules, {"stripe": up.stripe_module()}))
        stack.enter_context(patch.object(_common, "_TABLE", MemoryTable()))
        stack.enter_context(patch.object(take_payment, "_send_sms", up.send_sms))
        stack.enter_context(patch.object(book_meeting, "_service", up.calendar))
        stack.enter_context(patch.object(create_ticket, "_client", up.zendesk))
        stack.enter_context(patch.object(log_lead, "httpx", up.crm_httpx()))
        audit = AuditLog(Path(tmp) / "audit.jsonl")
        steps, failures, llm_saw = asyncio.run(_run_steps(scenario, up, audit))
        failures += _check(scenario, up, audit, llm_saw)
    return ScenarioResult(
        id=scenario["id"],
        title=scenario["title"],
        risk=scenario.get("risk", ""),
        passed=not failures,
        steps=steps,
        failures=failures,
    )


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    ids = [s["id"] for s in data["scenarios"]]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    return data["scenarios"]


def run_all(scenarios: list[dict] | None = None, only: list[str] | None = None) -> list[ScenarioResult]:
    scenarios = scenarios if scenarios is not None else load_scenarios()
    if only:
        scenarios = [s for s in scenarios if s["id"] in only]
    return [run_scenario(s) for s in scenarios]


# ---------- Reporting ----------


def scorecard_markdown(results: list[ScenarioResult]) -> str:
    passed = sum(r.passed for r in results)
    lines = [
        "## Voice agent call evals",
        "",
        f"**{passed}/{len(results)} scenarios passed**",
        "",
        "| | Scenario | Risk covered | Tool outcomes |",
        "|---|---|---|---|",
    ]
    for r in results:
        outcomes = " → ".join(f"`{s.got}`" for s in r.steps)
        lines.append(f"| {'✅' if r.passed else '❌'} | {r.title} | {r.risk} | {outcomes} |")
    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "### Failures", ""]
        for r in failed:
            lines.append(f"**{r.id}**")
            lines += [f"- {f}" for f in r.failures]
            lines.append("")
    return "\n".join(lines) + "\n"


def results_json(results: list[ScenarioResult]) -> str:
    return json.dumps(
        {
            "passed": sum(r.passed for r in results),
            "total": len(results),
            "scenarios": [
                {
                    "id": r.id,
                    "title": r.title,
                    "risk": r.risk,
                    "passed": r.passed,
                    "failures": r.failures,
                    "steps": [s.__dict__ for s in r.steps],
                }
                for r in results
            ],
        },
        indent=2,
    )
