# Corey Mathie, 2026
"""
Agent-callable tools.

Three layers:
  1. TOOL_SPECS        - what the LLM sees (name, description, JSON-schema params)
  2. raw executors     - thin HTTPS calls to the Lambda handlers behind API Gateway
  3. make_handler()    - the Pipecat function-call handler the LLM actually invokes.
                         Every call goes through velocity checks, PII-redacted audit
                         logging, and an idempotency key before anything executes.

The caller's phone number is bound into each handler per call (from the Twilio
stream's custom parameters), so velocity limits are per caller, not global.

Outcomes the LLM can get back from a tool:
  ok statuses     whatever the Lambda returns (booked, link_sent, created, logged, duplicate)
  "rejected"      the request was invalid (e.g. over the payment limit); `reason` says why,
                  so the agent can explain or ask again
  "denied"        velocity limit hit
  "require_human" a person must approve this
  "error"         upstream failure; details go to the audit log, never to the LLM
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.safeguards.audit_log import AuditLog
from src.safeguards.pii_redactor import redact
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
]

TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


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


async def _post(path: str, payload: dict, idempotency_key: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{LAMBDA_BASE}{path}",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
    if 400 <= r.status_code < 500:
        raise ToolRejected(_error_message(r))
    r.raise_for_status()
    return r.json()


def _executor(path: str) -> Callable[[dict, str], Awaitable[dict]]:
    async def run(args: dict, idempotency_key: str) -> dict:
        return await _post(path, args, idempotency_key)

    return run


EXECUTORS: dict[str, Callable[[dict, str], Awaitable[dict]]] = {name: _executor(f"/{name}") for name in TOOL_NAMES}


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


# Government and financial identifiers never leave the agent, even inside free text
# such as a ticket body. Emails and phone numbers are kept: the tools need them.
_KEEP_FOR_TOOLS = ("email", "phone")


def scrub_args(args: dict) -> tuple[dict, dict[str, int]]:
    """Redact card numbers, SSNs, bank details, DOBs, and license numbers in string arguments."""
    clean: dict = {}
    counts: dict[str, int] = {}
    for k, v in args.items():
        if isinstance(v, str):
            v, c = redact(v, allow=_KEEP_FOR_TOOLS)
            for rule, n in c.items():
                counts[rule] = counts.get(rule, 0) + n
        clean[k] = v
    return clean, counts


def make_handler(
    name: str,
    caller_id: str,
    executors: dict | None = None,
    *,
    velocity: VelocityStore | None = None,
    audit: AuditLog | None = None,
    outcomes: list[dict] | None = None,
):
    """
    Build the Pipecat function-call handler for one tool, bound to one caller.

    Pipecat 1.x calls handler(params: FunctionCallParams); we read params.arguments
    and deliver the result with params.result_callback. `outcomes`, if given,
    collects {"tool", "status"} for each call (used for the post-call summary).
    """
    raw = (executors or EXECUTORS)[name]
    ref = caller_ref(caller_id)
    store = velocity or _velocity

    async def handler(params) -> None:
        log = audit or get_audit()
        # The tool call id from the LLM makes the idempotency key stable across retries.
        idem = getattr(params, "tool_call_id", None) or str(uuid.uuid4())

        async def finish(result: dict) -> None:
            if outcomes is not None:
                outcomes.append({"tool": name, "status": result.get("status")})
            await params.result_callback(result)

        args = dict(params.arguments or {})
        if name == "take_payment":
            # Payment links only ever go to the number that is calling, whatever the model asks for.
            args.pop("customer_phone", None)
            if caller_id != "unknown":
                args["customer_phone"] = caller_id

        allowed, reason = store.check(caller_id, name, request_id=idem)
        if not allowed:
            log.append("velocity_denied", {"tool": name, "caller_ref": ref, "reason": reason})
            await finish({"status": "denied", "reason": "Too many requests. A person will follow up."})
            return

        args, blocked = scrub_args(args)
        redacted_args, pii_counts = redact(str(args))
        log.append(
            "tool_call",
            {
                "tool": name,
                "caller_ref": ref,
                "args_redacted": redacted_args,
                "pii_counts": pii_counts,
                "pii_removed_before_send": blocked,
                "velocity_note": reason,
                "idempotency_key": idem,
            },
        )

        if reason and "require_human" in reason:
            log.append("handoff_required", {"tool": name, "caller_ref": ref})
            await finish({"status": "require_human", "reason": "A person needs to approve this."})
            return

        try:
            result = await raw(args, idem)
        except ToolRejected as e:
            store.release(caller_id, name, idem)  # nothing happened; don't count it
            log.append("tool_rejected", {"tool": name, "caller_ref": ref, "reason": str(e)})
            await finish({"status": "rejected", "reason": str(e)})
            return
        except Exception as e:  # noqa: BLE001 - any failure: safe message to the LLM, detail to the audit log
            store.release(caller_id, name, idem)  # our outage shouldn't use up the caller's attempts
            log.append("tool_error", {"tool": name, "caller_ref": ref, "error": str(e)[:200]})
            await finish({"status": "error", "reason": "That didn't go through. Please try again in a moment."})
            return

        log.append("tool_result", {"tool": name, "caller_ref": ref, "status": result.get("status")})
        await finish(result)

    return handler
