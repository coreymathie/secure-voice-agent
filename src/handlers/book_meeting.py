# Corey Mathie, 2026
"""
book_meeting Lambda: creates an event on the business Google Calendar.

GOOGLE_SERVICE_ACCOUNT_JSON may be either the JSON content (Lambda, loaded from
Secrets Manager) or a file path (local development).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
REQUIRED = ("caller_name", "caller_email", "start_iso", "topic")


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    if raw.lstrip().startswith("{"):
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


@idempotent
def handler(event, _context):
    body = payload_of(event)
    missing = [k for k in REQUIRED if not body.get(k)]
    if missing:
        return bad(f"missing fields: {missing}")

    try:
        start = datetime.fromisoformat(body["start_iso"])
    except ValueError:
        return bad("start_iso must be ISO 8601, e.g. 2026-10-06T14:00:00-04:00")
    end = start + timedelta(minutes=int(body.get("duration_minutes") or 30))
    when = {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    if start.tzinfo is None:
        # Callers say "2pm Tuesday", so the model usually sends a local time with no offset.
        # Google Calendar rejects those without a timeZone, so use the business's own.
        tz = os.environ.get("BUSINESS_TIMEZONE", "America/New_York")
        when["start"]["timeZone"] = when["end"]["timeZone"] = tz

    created = (
        _service()
        .events()
        .insert(
            calendarId=os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
            body={
                "summary": f"{body['topic']} with {body['caller_name']}",
                "description": f"Booked by the voice agent.\nCaller: {body['caller_name']} <{body['caller_email']}>",
                **when,
                "attendees": [{"email": body["caller_email"]}],
            },
            sendUpdates="all",
        )
        .execute()
    )

    return ok({"status": "booked", "event_id": created["id"], "start": start.isoformat()})
