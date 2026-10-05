# Corey Mathie, 2026
"""
keypad_payment Lambda (PAYMENT_MODE=keypad): moves the live call from the agent's
media stream to Twilio <Pay>, so the caller types their card on the keypad and
the digits go to the payment connector, never to the agent.

Steps, in order, failing closed:
  1. Validate the amount (same rules as take_payment) and the call SID.
  2. Pause any in-progress call recording (Twilio.CURRENT). If there is no
     recording, carry on; if pausing fails for any other reason, stop: card
     digits must not be collected while a recording might be running.
  3. Redirect the call with <Say> + <Pay> TwiML (pay_twiml.build_pay_twiml).
     The redirect ends the <Connect><Stream>, so the agent's audio stream
     carries nothing while the card is entered.

The call comes back to the agent through twilio_pay_result.py (the <Pay>
action URL), which resumes the recording if this handler paused it.

Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, PAY_ACTION_URL (public URL of
the twilio_pay_result route), and a Pay Connector configured in Twilio
(PAY_CONNECTOR, default "Default"). Twilio <Pay> also needs PCI mode enabled on
the Twilio account; see docs/pci.md.
"""

from __future__ import annotations

import logging
import os
import re

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
    from .pay_twiml import KeypadConfig, build_pay_twiml
    from .take_payment import validate_amount
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of
    from pay_twiml import KeypadConfig, build_pay_twiml
    from take_payment import validate_amount

log = logging.getLogger(__name__)
CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")


class RecordingPauseFailed(Exception):
    pass


def _twilio():
    from twilio.rest import Client

    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _config() -> KeypadConfig:
    return KeypadConfig(
        connector=os.environ.get("PAY_CONNECTOR", "Default"),
        postal_code=os.environ.get("PAY_POSTAL_CODE", "false").lower() == "true",
    )


def pause_recording(client, call_sid: str) -> bool:
    """Pause the call's current recording. False if nothing was recording; raises if pausing failed."""
    try:
        client.calls(call_sid).recordings("Twilio.CURRENT").update(status="paused")
    except Exception as e:
        if getattr(e, "status", None) == 404:  # no recording in progress
            return False
        raise RecordingPauseFailed(str(e)[:200]) from e
    return True


@idempotent
def handler(event, _context):
    body = payload_of(event)
    for field in ("amount_usd", "description"):
        if field not in body:
            return bad(f"missing field: {field}")
    amount, problem = validate_amount(body)
    if problem:
        return bad(problem)
    call_sid = str(body.get("call_sid") or "")
    if not CALL_SID.match(call_sid):
        return bad("keypad payments need the live call; ask the caller to stay on the line")
    action_url = os.environ.get("PAY_ACTION_URL", "")
    if not action_url.startswith("https://"):
        raise RuntimeError("PAY_ACTION_URL is not configured")

    client = _twilio()
    # Raises (-> generic error to the agent, key released) if a recording might still be running.
    paused = pause_recording(client, call_sid)
    twiml = build_pay_twiml(amount, body["description"], action_url, _config(), resume_recording=paused)
    client.calls(call_sid).update(twiml=twiml)
    log.info("keypad payment started for %s (recording %s)", call_sid, "paused" if paused else "not running")
    return ok(
        {
            "status": "keypad_started",
            "recording": "paused" if paused else "not_recording",
            "message": "The caller is now entering their card on the keypad. The call returns to you afterwards.",
        }
    )
