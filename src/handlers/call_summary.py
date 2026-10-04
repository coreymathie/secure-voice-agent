# Corey Mathie, 2026
"""
call_summary Lambda: attaches the post-call summary to the caller's CRM contact
as a note (GoHighLevel), creating the contact by phone number if needed.

Idempotent on the call SID, so a retried delivery doesn't add the note twice.
Card numbers and other identifiers are scrubbed again here before anything is
written, in case a summary quoted one.
"""

from __future__ import annotations

import os
import re

import httpx

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of

GHL_BASE = "https://services.leadconnectorhq.com"
OUTCOME_LABELS = {
    "resolved": "Resolved",
    "follow_up": "Needs follow-up",
    "escalated": "Escalated to a person",
    "abandoned": "Caller hung up early",
}
# Same identifiers the agent scrubs (card numbers, SSNs); kept local so the Lambda bundle stays small.
_IDENTIFIERS = (
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_PAN]"),
    (re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b"), "[REDACTED_SSN]"),
)


def _scrub(text: str) -> str:
    for pattern, replacement in _IDENTIFIERS:
        text = pattern.sub(replacement, text)
    return text


def format_note(body: dict) -> str:
    lines = [
        f"Voice agent call · {OUTCOME_LABELS.get(body['outcome'], body['outcome'])}",
        f"Intent: {body.get('caller_intent') or 'unknown'} · Sentiment: {body.get('sentiment', 'neutral')}",
        "",
        body["summary"],
    ]
    if body.get("follow_ups"):
        lines += ["", "Follow-ups:"] + [f"- {f}" for f in body["follow_ups"]]
    if body.get("tools"):
        lines += ["", "Actions: " + "; ".join(f"{t['tool']} → {t['status']}" for t in body["tools"])]
    lines += ["", f"Call SID: {body['call_sid']}"]
    return _scrub("\n".join(lines))


@idempotent
def handler(event, _context):
    body = payload_of(event)
    for required in ("call_sid", "summary", "outcome"):
        if not body.get(required):
            return bad(f"missing field: {required}")

    note = format_note(body)
    phone = body.get("caller_phone") or ""
    api_key, location = os.environ.get("GOHIGHLEVEL_API_KEY"), os.environ.get("GOHIGHLEVEL_LOCATION_ID")
    if not (api_key and location):
        return ok({"status": "skipped", "reason": "no CRM configured", "note": note})
    if not phone or phone == "unknown":
        return ok({"status": "skipped", "reason": "no caller number to match a contact"})

    headers = {"Authorization": f"Bearer {api_key}", "Version": "2021-07-28", "Accept": "application/json"}
    with httpx.Client(timeout=10.0) as client:
        r = client.post(
            f"{GHL_BASE}/contacts/upsert",
            json={"locationId": location, "phone": phone, "source": "voice-agent"},
            headers=headers,
        )
        r.raise_for_status()
        contact_id = r.json()["contact"]["id"]
        r = client.post(f"{GHL_BASE}/contacts/{contact_id}/notes", json={"body": note}, headers=headers)
        r.raise_for_status()

    return ok({"status": "saved", "contact_id": contact_id, "note_id": (r.json().get("note") or {}).get("id")})
