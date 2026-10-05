# Corey Mathie, 2026
"""
Agent-callable tools.

Three layers:
  1. TOOL_SPECS        - what the LLM sees (name, description, JSON-schema params)
  2. raw executors     - thin HTTPS calls to the Lambda handlers behind API Gateway
  3. make_handler()    - the Pipecat function-call handler the LLM actually invokes.
                         Every call goes through the policy gate (allow-list, caller
                         risk score, per-call caps), velocity checks, PII-redacted
                         audit logging, and an idempotency key before anything executes.

The caller's phone number is bound into each handler per call (from the Twilio
stream's custom parameters), so velocity limits are per caller, not global.

Step-up verification (send_verification_code, verify_caller) runs in this
process against a Verifier (src/safeguards/step_up.py), still through
make_handler, so the same gate, velocity limits, and audit apply. The code a
caller reads back is masked before anything is logged.

Outcomes the LLM can get back from a tool:
  ok statuses     whatever the Lambda returns (booked, link_sent, created, logged, updated, duplicate),
                  or code_sent / verified / invalid_code from step-up verification
  "step_up_required" the caller must be verified first (offer a one-time code)
  "rejected"      the request was invalid (e.g. over the payment limit); `reason` says why,
                  so the agent can explain or ask again
  "denied"        velocity limit hit, or the policy gate refused the tool (not allow-listed)
  "require_human" a person must approve this (velocity escalation, caller risk score,
                  or a per-call cap); `reason` tells the agent what to say
  "error"         upstream failure; details go to the audit log, never to the LLM
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.safeguards.audit_log import AuditLog
from src.safeguards.pii_redactor import TOOL_ARG_ALLOW, redact, scrub_args
from src.safeguards.policy_gate import CallPolicy
from src.safeguards.step_up import Rejected as StepUpRejected
from src.safeguards.step_up import StepUpSession
from src.safeguards.telemetry import record_safeguard_event
from src.safeguards.velocity import VelocityStore

LAMBDA_BASE = os.environ.get("LAMBDA_BASE_URL", "")

# ---------- 1. Tool specs (what the LLM sees) ----------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "book_meeting",
        "description": "Schedule a meeting on the business calendar.",
        "properties": {
            "caller_name": {"type": "string"},
            "caller_email": {"type": "string"},
            "start_iso": {"type": "string", "description": "ISO 8601 start time"},
            "duration_minutes": {"type": "integer", "description": "Defaults to 30"},
            "topic": {"type": "string"},
        },
        "required": ["caller_name", "caller_email", "start_iso", "topic"],
    },
    {
        "name": "take_payment",
        "description": (
            "Text the caller a secure payment link for a stated amount in USD. "
            "Never ask the caller to read out card numbers."
        ),
        "properties": {
            "amount_usd": {"type": "number"},
            "description": {"type": "string"},
            "customer_email": {"type": "string"},
        },
        "required": ["amount_usd", "description", "customer_email"],
    },
    {
        "name": "create_ticket",
        "description": "Open a support ticket for the caller.",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "caller_email": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
        },
        "required": ["subject", "body", "caller_email"],
    },
    {
        "name": "log_lead",
        "description": "Save the caller's details as a sales lead.",
        "properties": {
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["first_name", "phone"],
    },
    {
        "name": "update_contact",
        "description": (
            "Change the email address or phone number on file for a verified caller. Only call this after the "
            "caller has been verified."
        ),
        "properties": {
            "new_email": {"type": "string"},
            "new_phone": {"type": "string", "description": "E.164, e.g. +15555550123"},
        },
        "required": [],
    },
    {
        "name": "send_verification_code",
        "description": (
            "Send the caller a one-time code at the phone number already on file, to verify them before a "
            "sensitive action. You can't choose the number."
        ),
        "properties": {"channel": {"type": "string", "enum": ["sms", "call"], "description": "Defaults to sms"}},
        "required": [],
    },
    {
        "name": "verify_caller",
        "description": "Check the one-time code the caller reads back after send_verification_code.",
        "properties": {"code": {"type": "string"}},
        "required": ["code"],
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_SPECS]
STEP_UP_TOOLS = ("send_verification_code", "verify_caller")  # handled in-process by a StepUpSession
LAMBDA_TOOLS = [n for n in TOOL_NAMES if n not in STEP_UP_TOOLS]
CUSTOMER_BOUND_TOOLS = ("update_contact",)  # customer_id comes from the verified session, never the model
SECRET_ARGS: dict[str, tuple[str, ...]] = {"verify_caller": ("code",)}  # masked before logging


def build_tools_schema():
    """Pipecat ToolsSchema built from TOOL_SPECS (imported lazily so tests run without Pipecat)."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name=t["name"],
                description=t["description"],
                properties=t["properties"],
                required=t["required"],
            )
            for t in TOOL_SPECS
        ]
    )


# ---------- 2. Raw executors (HTTPS to Lambda) ----------


class ToolRejected(Exception):
    """The handler refused the request (HTTP 4xx). The message is safe to show the LLM."""


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or "the request was not accepted")[:200]
    except ValueError:
        return "the request was not accepted"


def tool_request(payload: dict, idempotency_key: str) -> tuple[bytes, dict[str, str]]:
    """Serialize a tool request and sign it (see src/handlers/_common.py, "Request signing")."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
    secret = os.environ.get("TOOL_API_SECRET", "")
    if secret:
        from src.handlers._common import signed_headers  # lazy: the browser demo never signs

        headers.update(signed_headers(secret, idempotency_key, body))
    return body, headers


async def _post(path: str, payload: dict, idempotency_key: str) -> dict:
    body, headers = tool_request(payload, idempotency_key)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(f"{LAMBDA_BASE}{path}", content=body, headers=headers)
    if 400 <= r.status_code < 500:
        raise ToolRejected(_error_message(r))
    r.raise_for_status()
    return r.json()


def _executor(path: str) -> Callable[[dict, str], Awaitable[dict]]:
    async def run(args: dict, idempotency_key: str) -> dict:
        return await _post(path, args, idempotency_key)

    return run


EXECUTORS: dict[str, Callable[[dict, str], Awaitable[dict]]] = {name: _executor(f"/{name}") for name in LAMBDA_TOOLS}


def lambda_executors(payment_mode: str = "link") -> dict[str, Callable[[dict, str], Awaitable[dict]]]:
    """
    The Lambda executors for a deployment. PAYMENT_MODE=keypad sends take_payment to the
    keypad_payment Lambda (Twilio <Pay>, docs/pci.md) instead of texting a payment link.
    The tool name, tier, step-up, caps, and velocity rules stay the same either way.
    """
    table = dict(EXECUTORS)
    if payment_mode == "keypad":
        table["take_payment"] = _executor("/keypad_payment")
    return table


def step_up_executors(session: StepUpSession) -> dict[str, Callable[[dict, str], Awaitable[dict]]]:
    """Executors for send_verification_code / verify_caller, bound to one call's StepUpSession."""

    def wrap(fn):
        async def run(args: dict, _idempotency_key: str) -> dict:
            try:
                if session.blocking:  # a real verification API: keep the audio loop responsive
                    return await asyncio.to_thread(fn, args)
                return fn(args)
            except StepUpRejected as e:
                raise ToolRejected(str(e)) from e

        return run

    return {"send_verification_code": wrap(session.send_code), "verify_caller": wrap(session.check_code)}


# ---------- 3. Safeguarded Pipecat handlers ----------

_audit: AuditLog | None = None
_velocity = VelocityStore()


def get_audit() -> AuditLog:
    """Lazily create the audit log so importing this module has no side effects."""
    global _audit
    if _audit is None:
        _audit = AuditLog(os.environ.get("AUDIT_LOG_PATH", "./logs/audit.jsonl"))
    return _audit


def caller_ref(caller_id: str) -> str:
    """Salted hash of the caller's number. The audit log never stores the raw number."""
    salt = os.environ.get("AUDIT_SALT", "change-me")
    return hashlib.sha256(f"{salt}:{caller_id}".encode()).hexdigest()[:16]


# Kept for backwards compatibility; the list lives with the redactor now.
_KEEP_FOR_TOOLS = TOOL_ARG_ALLOW


def make_handler(
    name: str | None,
    caller_id: str,
    executors: dict | None = None,
    *,
    velocity: VelocityStore | None = None,
    audit: AuditLog | None = None,
    outcomes: list[dict] | None = None,
    policy: CallPolicy | None = None,
    on_result: Callable[[str, dict], None] | None = None,
):
    """
    Build the Pipecat function-call handler for one tool, bound to one caller.

    `on_result(tool, result)`, if given, sees every result the model gets (bot.py uses
    it to notice a keypad payment starting).

    Pipecat 1.x calls handler(params: FunctionCallParams); we read params.arguments
    and deliver the result with params.result_callback. `outcomes`, if given,
    collects {"tool", "status"} for each call (used for the post-call summary).

    `policy` is the call's CallPolicy. Pass the same one to every handler of a call
    so the per-call caps and the caller's risk score span all tools; bot.py does.
    Without one, each handler gets its own (allow-list and risk checks still apply).

    `name=None` builds a catch-all handler that reads the tool name from
    params.function_name. Registered as Pipecat's catch-all, it routes any tool the
    model invents to the policy gate, which denies it.
    """
    table = EXECUTORS if executors is None else executors
    ref = caller_ref(caller_id)
    store = velocity or _velocity
    gate = policy or CallPolicy.from_env()

    async def handler(params) -> None:
        log = audit or get_audit()
        tool = name or str(getattr(params, "function_name", "") or "<unnamed>")
        # The tool call id from the LLM makes the idempotency key stable across retries.
        idem = getattr(params, "tool_call_id", None) or str(uuid.uuid4())
        listed = tool in gate.config.tool_policies
        span_attrs = {"tool.name": tool if listed else "<unlisted>", "conversation.id": gate.call_id}

        async def finish(result: dict) -> None:
            if outcomes is not None:
                outcomes.append({"tool": tool, "status": result.get("status")})
            if on_result is not None:
                on_result(tool, result)
            await params.result_callback(result)

        args = dict(params.arguments or {})
        if tool == "take_payment":
            # Payment links only ever go to the number that is calling, whatever the model asks for.
            args.pop("customer_phone", None)
            if caller_id != "unknown":
                args["customer_phone"] = caller_id
            # Keypad mode redirects this call (Twilio <Pay>); the call is the agent's, not the model's, to name.
            args.pop("call_sid", None)
            if gate.call_id:
                args["call_sid"] = gate.call_id
        if tool in CUSTOMER_BOUND_TOOLS:
            # Which record changes is decided by step-up verification, not by the model.
            args.pop("customer_id", None)
            if gate.customer_id:
                args["customer_id"] = gate.customer_id

        # 0. Policy gate: allow-list, social-engineering risk, per-call blast radius.
        decision = gate.decide(tool, args)
        record_safeguard_event(
            "safeguard.policy",
            {
                **span_attrs,
                "safeguard.decision": decision.action,
                "safeguard.code": decision.code,
                "policy.tier": decision.tier,
                "policy.risk_score": decision.risk_score,
                "policy.risk_signals": list(decision.risk_signals),
            },
        )
        if not decision.allowed:
            event = {"deny": "policy_denied", "step_up": "policy_step_up"}.get(decision.action, "policy_handoff")
            log.append(event, {"tool": decision.tool, "caller_ref": ref, **decision.audit_fields()})
            await finish(decision.result())
            return
        raw = table.get(tool)
        if raw is None:  # declared in policy but not wired to a backend in this deployment
            log.append("policy_denied", {"tool": tool, "caller_ref": ref, "code": "no_executor"})
            await finish({"status": "denied", "reason": "That action isn't available on this line."})
            return
        policy_note = decision.audit_fields()

        allowed, reason = store.check(caller_id, tool, request_id=idem)
        record_safeguard_event(
            "safeguard.velocity",
            {
                **span_attrs,
                "safeguard.decision": "deny" if not allowed else ("handoff" if reason else "allow"),
            },
        )
        if not allowed:
            log.append("velocity_denied", {"tool": tool, "caller_ref": ref, "reason": reason, "policy": policy_note})
            await finish({"status": "denied", "reason": "Too many requests. A person will follow up."})
            return

        args, blocked = scrub_args(args)
        loggable = {k: ("[REDACTED_SECRET]" if k in SECRET_ARGS.get(tool, ()) else v) for k, v in args.items()}
        redacted_args, pii_counts = redact(str(loggable))
        if blocked:
            record_safeguard_event(
                "safeguard.pii_scrubbed",
                {**span_attrs, "pii.removed": sum(blocked.values()), "pii.rules": sorted(blocked)},
            )
        log.append(
            "tool_call",
            {
                "tool": tool,
                "caller_ref": ref,
                "args_redacted": redacted_args,
                "pii_counts": pii_counts,
                "pii_removed_before_send": blocked,
                "velocity_note": reason,
                "policy": policy_note,
                "idempotency_key": idem,
            },
        )

        if reason and "require_human" in reason:
            log.append("handoff_required", {"tool": tool, "caller_ref": ref})
            await finish({"status": "require_human", "reason": "A person needs to approve this."})
            return

        try:
            result = await raw(args, idem)
        except ToolRejected as e:
            store.release(caller_id, tool, idem)  # nothing happened; don't count it
            log.append("tool_rejected", {"tool": tool, "caller_ref": ref, "reason": str(e)})
            await finish({"status": "rejected", "reason": str(e)})
            return
        except Exception as e:  # noqa: BLE001 - any failure: safe message to the LLM, detail to the audit log
            store.release(caller_id, tool, idem)  # our outage shouldn't use up the caller's attempts
            log.append("tool_error", {"tool": tool, "caller_ref": ref, "error": str(e)[:200]})
            await finish({"status": "error", "reason": "That didn't go through. Please try again in a moment."})
            return

        result = dict(result)
        detail = result.pop("_audit", None)  # audit-only detail (e.g. why a code wasn't sent); never shown to the model
        gate.record_outcome(tool, args, result.get("status"))
        record_safeguard_event("safeguard.tool_result", {**span_attrs, "tool.status": str(result.get("status"))})
        entry = {"tool": tool, "caller_ref": ref, "status": result.get("status")}
        extra_event = None
        if isinstance(detail, dict):
            detail = dict(detail)
            extra_event = detail.pop("event", None)
            entry["detail"] = detail
        log.append("tool_result", entry)
        if extra_event:
            log.append(str(extra_event), {"tool": tool, "caller_ref": ref, **(detail or {})})
        await finish(result)

    return handler


def make_fallback_handler(caller_id: str, **kw):
    """
    Catch-all for tool names the model invents. Register it with
    `llm.register_function(None, ...)`; the policy gate denies anything without a
    declared policy and the attempt is audited.
    """
    return make_handler(None, caller_id, **kw)
