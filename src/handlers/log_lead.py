# Corey Mathie, 2026
"""
log_lead Lambda — upserts a contact in GoHighLevel.
"""

from __future__ import annotations

import os

import httpx

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of

GHL_BASE = "https://services.leadconnectorhq.com"


@idempotent
def handler(event, _context):
    body = payload_of(event)
    for required in ("first_name", "phone"):
        if required not in body:
            return bad(f"missing field: {required}")

    headers = {
        "Authorization": f"Bearer {os.environ['GOHIGHLEVEL_API_KEY']}",
        "Version": "2021-07-28",
        "Accept": "application/json",
    }
    payload = {
        "locationId": os.environ["GOHIGHLEVEL_LOCATION_ID"],
        "firstName": body["first_name"],
        "lastName": body.get("last_name", ""),
        "phone": body["phone"],
        "email": body.get("email"),
        "source": "voice-agent",
        "tags": ["voice-agent", "inbound-call"],
        "customField": [{"key": "voice_agent_notes", "field_value": body.get("notes", "")}],
    }

    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{GHL_BASE}/contacts/upsert", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()

    return ok({"status": "logged", "contact_id": data.get("contact", {}).get("id")})
