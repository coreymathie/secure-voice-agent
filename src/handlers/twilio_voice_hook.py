# Corey Mathie, 2026
"""
Twilio voice webhook on AWS Lambda (API Gateway HTTP API).

1. Verifies the X-Twilio-Signature header so only Twilio can trigger calls.
2. Returns TwiML that plays the fixed AI disclosure (and, when the call may be
   recorded, a recording-consent prompt; see call_start.py), then opens a media
   stream to the long-lived Pipecat agent, passing the caller's number and the
   consent result as custom stream parameters.
"""

from __future__ import annotations

import base64
import os
from urllib.parse import parse_qsl

from twilio.request_validator import RequestValidator

try:  # package import (tests, local dev)
    from .call_start import CallStartConfig, build_voice_twiml
except ImportError:  # Lambda imports handlers as top-level modules
    from call_start import CallStartConfig, build_voice_twiml


def _form(event: dict) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return dict(parse_qsl(body, keep_blank_values=True))


def _public_url(event: dict) -> str:
    ctx = event.get("requestContext", {})
    domain = ctx.get("domainName", "")
    path = event.get("rawPath", "/voice")
    query = event.get("rawQueryString", "")
    return f"https://{domain}{path}" + (f"?{query}" if query else "")


def sibling_url(event: dict, route: str) -> str:
    """URL of another route on the same API (e.g. /prod/voice -> /prod/consent)."""
    ctx = event.get("requestContext", {})
    path = event.get("rawPath", "/voice")
    base = path.rsplit("/", 1)[0]
    return f"https://{ctx.get('domainName', '')}{base}/{route}"


def verified_form(event: dict) -> dict | None:
    """The form body if the request is signed by Twilio (or no auth token is configured); otherwise None."""
    form = _form(event)
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if auth_token:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        signature = headers.get("x-twilio-signature", "")
        if not RequestValidator(auth_token).validate(_public_url(event), form, signature):
            return None
    return form


def handler(event, _context):
    form = verified_form(event)
    if form is None:
        return {"statusCode": 403, "body": "invalid Twilio signature"}
    consent_url = os.environ.get("CONSENT_ACTION_URL") or sibling_url(event, "consent")
    twiml = build_voice_twiml(os.environ["AGENT_PUBLIC_WS_URL"], consent_url, form, CallStartConfig.from_env())
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/xml"},
        "body": twiml,
    }
