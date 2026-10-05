# Corey Mathie, 2026
"""
twilio_pay_result Lambda: the <Pay> action URL. Twilio calls it when the caller
finishes (or abandons) entering their card on the keypad.

1. Verifies X-Twilio-Signature (same as the voice webhook).
2. Reads only Result and PaymentConfirmationCode (pay_twiml.parse_pay_result);
   the masked card number, expiry, and any token in the callback are ignored.
3. Resumes the call recording if keypad_payment paused it (?resume_recording=1).
   A failure here is logged and doesn't stop the caller getting back to the agent.
4. Returns TwiML that tells the caller the outcome and reconnects the call to the
   agent's media stream with `resume=keypad_payment` and the result code, so the
   voice process can pick the conversation back up.
"""

from __future__ import annotations

import logging
import os

from twilio.request_validator import RequestValidator

try:  # package import (tests, local dev)
    from .pay_twiml import build_resume_twiml, parse_pay_result
    from .twilio_voice_hook import _form, _public_url
except ImportError:  # Lambda imports handlers as top-level modules
    from pay_twiml import build_resume_twiml, parse_pay_result
    from twilio_voice_hook import _form, _public_url

log = logging.getLogger(__name__)


def _twilio():
    from twilio.rest import Client

    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def resume_recording(client, call_sid: str) -> bool:
    try:
        client.calls(call_sid).recordings("Twilio.CURRENT").update(status="in-progress")
        return True
    except Exception as e:  # noqa: BLE001 - never strand the caller over a recording API error
        log.error("could not resume recording on %s: %s", call_sid, str(e)[:200])
        return False


def handler(event, _context):
    form = _form(event)
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if auth_token:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        if not RequestValidator(auth_token).validate(_public_url(event), form, headers.get("x-twilio-signature", "")):
            return {"statusCode": 403, "body": "invalid Twilio signature"}

    outcome = parse_pay_result(form)
    query = event.get("queryStringParameters") or {}
    call_sid = form.get("CallSid", "")
    if query.get("resume_recording") == "1" and call_sid and outcome.result != "caller-hung-up":
        resume_recording(_twilio(), call_sid)
    log.info("keypad payment result for %s: %s", call_sid, outcome.result)
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/xml"},
        "body": build_resume_twiml(os.environ["AGENT_PUBLIC_WS_URL"], form.get("From", "unknown"), outcome),
    }
