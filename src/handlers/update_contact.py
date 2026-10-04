# Corey Mathie, 2026
"""
update_contact Lambda: changes the email and/or phone number on a GoHighLevel
contact.

The voice process only calls this for a caller who passed step-up verification,
and it sets `customer_id` from the verified session (the model can't choose
which record changes). The policy gate then treats any payment later in the same
call as an account-takeover pattern and hands it to a person.

Not implemented here: notifying the *previous* contact that details changed (a
common takeover mitigation). Add it in your CRM's workflow or below.
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
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


@idempotent
def handler(event, _context):
    body = payload_of(event)
    customer_id = str(body.get("customer_id") or "")
    if not customer_id:
        return bad("the caller isn't verified, so there's no contact record to change")
    if not _ID.match(customer_id):
        return bad("invalid customer_id")
    changes: dict[str, str] = {}
    if body.get("new_email"):
        email = str(body["new_email"]).strip()
        if not _EMAIL.match(email):
            return bad("new_email doesn't look like an email address; ask the caller to spell it")
        changes["email"] = email
    if body.get("new_phone"):
        phone = re.sub(r"[\s().-]", "", str(body["new_phone"]))
        if not _E164.match(phone):
            return bad("new_phone must be a full number with country code, like +15555550123")
        changes["phone"] = phone
    if not changes:
        return bad("nothing to change: give new_email or new_phone")

    headers = {
        "Authorization": f"Bearer {os.environ['GOHIGHLEVEL_API_KEY']}",
        "Version": "2021-07-28",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        r = client.put(f"{GHL_BASE}/contacts/{customer_id}", json=changes, headers=headers)
        r.raise_for_status()

    return ok({"status": "updated", "fields": sorted(changes)})
