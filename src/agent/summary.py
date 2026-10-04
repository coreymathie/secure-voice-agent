# Corey Mathie, 2026
"""
Post-call summaries.

When a call ends, the conversation is turned into a short, structured note:
what the caller wanted, what happened, what still needs doing, and every tool
outcome. The note goes to the audit log and to the `call_summary` Lambda, which
attaches it to the caller's CRM contact. Staff see "called about a roof estimate,
booked Tuesday 2pm, wants a quote emailed" instead of nothing.

Privacy: the transcript is PII-redacted before it is sent to the summarizing
model, and the tool outcomes are passed as facts (the model doesn't have to
guess whether a booking went through).

Backends: Anthropic (tool-forced JSON) or OpenAI (JSON schema), chosen by
SUMMARY_PROVIDER or by whichever API key is present. With neither, or if the
model call fails, a rule-based summary is written instead, so a call never ends
without a record.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from src.safeguards.audit_log import AuditLog
from src.safeguards.pii_redactor import redact

log = logging.getLogger(__name__)

# The bot seeds the conversation with this so the agent greets first; it isn't the caller speaking.
KICKOFF_MESSAGE = "The caller just connected. Greet them warmly and briefly."
# After a keypad payment (Twilio <Pay>) the call reconnects on a new stream; this resumes the conversation.
RESUME_PREFIX = "The caller is back from entering their card on the keypad."
RESUME_KICKOFF = (
    RESUME_PREFIX + " Payment result: {result}. Tell them the outcome in one short sentence and ask if they need "
    "anything else. Never ask for card details."
)


def is_kickoff(text: str) -> bool:
    """Messages the bot injects (start, resume, call-limit instructions); they aren't the caller speaking."""
    from .call_limits import CONTROL_PREFIX

    return text == KICKOFF_MESSAGE or text.startswith(RESUME_PREFIX) or text.startswith(CONTROL_PREFIX)


OUTCOMES = ("resolved", "follow_up", "escalated", "abandoned")
SENTIMENTS = ("positive", "neutral", "negative")
SUCCESS_STATUSES = {"booked", "link_sent", "link_created", "created", "logged", "duplicate", "updated", "keypad_paid"}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 plain sentences: what the caller wanted and what happened."},
        "caller_intent": {"type": "string", "description": "A few words, e.g. 'book a roof estimate'."},
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "follow_ups": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete next steps for staff. Empty if none.",
        },
        "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
    },
    "required": ["summary", "caller_intent", "outcome", "follow_ups", "sentiment"],
    "additionalProperties": False,
}

SYSTEM = (
    "You write call notes for a small business's CRM from a phone agent's transcript. "
    "Be factual and brief. Use the tool outcomes as the record of what actually happened; "
    "never claim something was booked, paid, or sent unless a tool outcome says so. "
    "outcome: 'resolved' if the caller got what they called for, 'follow_up' if staff need to do "
    "something, 'escalated' if a request was denied or needs a person's approval, 'abandoned' if "
    "the caller hung up before saying what they wanted. Personal details in the transcript are "
    "redacted; don't guess them."
)


@dataclass
class CallSummary:
    summary: str
    caller_intent: str
    outcome: str
    follow_ups: list[str]
    sentiment: str
    tools: list[dict] = field(default_factory=list)
    caller_turns: int = 0
    generated_by: str = "rules"


# ---------- Transcript ----------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def transcript_from_messages(messages: list) -> list[tuple[str, str]]:
    """(role, text) turns from Pipecat LLMContext messages; skips system, tool, and provider-specific entries."""
    turns: list[tuple[str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        text = _text_of(m.get("content")).strip()
        if text and not is_kickoff(text):
            turns.append((m["role"], text))
    return turns


def redacted_transcript(turns: list[tuple[str, str]]) -> str:
    lines = []
    for role, text in turns:
        clean, _ = redact(text)
        lines.append(f"{'Caller' if role == 'user' else 'Agent'}: {clean}")
    return "\n".join(lines)


# ---------- Summarizing ----------


def _backend() -> str:
    choice = os.environ.get("SUMMARY_PROVIDER", "auto").lower()
    if choice != "auto":
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "none"


def _prompt(transcript: str, outcomes: list[dict]) -> str:
    facts = "\n".join(f"- {o['tool']}: {o['status']}" for o in outcomes) or "- none"
    return f"Tool outcomes during the call:\n{facts}\n\nTranscript:\n{transcript or '(no speech captured)'}"


async def _anthropic(prompt: str, client: httpx.AsyncClient) -> tuple[dict, str]:
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5")
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 700,
            "system": SYSTEM,
            "tools": [
                {"name": "record_call_summary", "description": "Save the call note.", "input_schema": SUMMARY_SCHEMA}
            ],
            "tool_choice": {"type": "tool", "name": "record_call_summary"},
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    r.raise_for_status()
    block = next(b for b in r.json()["content"] if b.get("type") == "tool_use")
    return block["input"], model


async def _openai(prompt: str, client: httpx.AsyncClient) -> tuple[dict, str]:
    model = os.environ.get("SUMMARY_MODEL", "gpt-4.1-mini")
    r = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "call_summary", "strict": True, "schema": SUMMARY_SCHEMA},
            },
        },
    )
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"]), model


def _validated(data: dict) -> dict:
    if data.get("outcome") not in OUTCOMES or data.get("sentiment") not in SENTIMENTS:
        raise ValueError(f"bad enum in summary: {data.get('outcome')!r}, {data.get('sentiment')!r}")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise ValueError("empty summary")
    follow_ups = data.get("follow_ups") or []
    return {
        "summary": data["summary"].strip(),
        "caller_intent": str(data.get("caller_intent", "")).strip(),
        "outcome": data["outcome"],
        "follow_ups": [str(f) for f in follow_ups][:10],
        "sentiment": data["sentiment"],
    }


def rule_based(turns: list[tuple[str, str]], outcomes: list[dict]) -> CallSummary:
    """A deterministic summary from tool outcomes alone. Used without an LLM, or when it fails."""
    caller_turns = sum(1 for role, _ in turns if role == "user")
    statuses = [o["status"] for o in outcomes]
    succeeded = {o["tool"] for o in outcomes if o["status"] in SUCCESS_STATUSES}
    unresolved = [o for o in outcomes if o["status"] in {"error", "rejected"} and o["tool"] not in succeeded]

    if any(s in {"require_human", "denied", "sms_failed"} for s in statuses):
        outcome = "escalated"
    elif unresolved:
        outcome = "follow_up"
    elif succeeded:
        outcome = "resolved"
    elif caller_turns <= 1:
        outcome = "abandoned"
    else:
        outcome = "follow_up"

    first = next((text for role, text in turns if role == "user"), "")
    intent = redact(first)[0][:80] if first else "unknown"
    done = ", ".join(f"{o['tool']} ({o['status']})" for o in outcomes) or "no actions taken"
    follow_ups = [f"Follow up on {o['tool']}: {o['status']}" for o in outcomes if o["status"] not in SUCCESS_STATUSES]
    return CallSummary(
        summary=f"Call with {caller_turns} caller turns. Actions: {done}.",
        caller_intent=intent,
        outcome=outcome,
        follow_ups=follow_ups,
        sentiment="neutral",
        tools=list(outcomes),
        caller_turns=caller_turns,
    )


async def summarize(
    turns: list[tuple[str, str]], outcomes: list[dict], client: httpx.AsyncClient | None = None
) -> CallSummary:
    backend = _backend()
    caller_turns = sum(1 for role, _ in turns if role == "user")
    if backend not in ("anthropic", "openai") or (not turns and not outcomes):
        return rule_based(turns, outcomes)

    prompt = _prompt(redacted_transcript(turns), outcomes)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        data, model = await (_anthropic if backend == "anthropic" else _openai)(prompt, client)
        fields = _validated(data)
    except Exception as e:  # noqa: BLE001 - any model failure falls back to the rule-based note
        log.warning("summary model failed (%s); using rule-based summary", e)
        return rule_based(turns, outcomes)
    finally:
        if own_client:
            await client.aclose()
    return CallSummary(**fields, tools=list(outcomes), caller_turns=caller_turns, generated_by=f"{backend}:{model}")


# ---------- Delivery ----------


async def deliver(
    summary: CallSummary,
    call_sid: str | None,
    caller_id: str,
    caller_ref: str,
    audit: AuditLog,
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    """Record the summary in the audit log and send it to the call_summary Lambda (CRM note)."""
    audit.append(
        "call_summary",
        {
            "caller_ref": caller_ref,
            "call_sid": call_sid,
            "outcome": summary.outcome,
            "summary": redact(summary.summary)[0],
            "follow_ups": [redact(f)[0] for f in summary.follow_ups],
            "tools": summary.tools,
            "generated_by": summary.generated_by,
        },
    )
    base = os.environ.get("LAMBDA_BASE_URL", "")
    if not base or not call_sid:
        return None
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        from .tools import tool_request

        body, headers = tool_request(
            {"call_sid": call_sid, "caller_phone": caller_id, **asdict(summary)}, f"summary-{call_sid}"
        )
        r = await client.post(f"{base}/call_summary", content=body, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 - delivery failure must not crash call teardown
        audit.append("call_summary_error", {"caller_ref": caller_ref, "call_sid": call_sid, "error": str(e)[:200]})
        return None
    finally:
        if own_client:
            await client.aclose()


async def finalize_call(messages: list, outcomes: list[dict], call_sid: str | None, caller_id: str) -> None:
    """Summarize and deliver. Never raises: this runs while the call is being torn down."""
    from .tools import caller_ref, get_audit

    if os.environ.get("CALL_SUMMARY_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    try:
        turns = transcript_from_messages(messages)
        summary = await summarize(turns, outcomes)
        await deliver(summary, call_sid, caller_id, caller_ref(caller_id), get_audit())
    except Exception:
        log.exception("post-call summary failed for %s", call_sid)
