# Corey Mathie, 2026
"""
create_ticket Lambda — opens a Zendesk ticket for the caller.
"""

from __future__ import annotations

import os

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of


def _client():
    from zenpy import Zenpy

    return Zenpy(
        subdomain=os.environ["ZENDESK_SUBDOMAIN"],
        email=os.environ["ZENDESK_EMAIL"],
        token=os.environ["ZENDESK_API_TOKEN"],
    )


@idempotent
def handler(event, _context):
    body = payload_of(event)
    for required in ("subject", "body", "caller_email"):
        if required not in body:
            return bad(f"missing field: {required}")

    from zenpy.lib.api_objects import Ticket, User

    zc = _client()
    requester = User(email=body["caller_email"], name=body.get("caller_name", body["caller_email"]))
    ticket = zc.tickets.create(
        Ticket(
            subject=body["subject"],
            description=body["body"],
            priority=body.get("priority", "normal"),
            requester=requester,
            tags=["voice-agent", "inbound"],
        )
    )
    return ok({"status": "created", "ticket_id": ticket.ticket.id})
