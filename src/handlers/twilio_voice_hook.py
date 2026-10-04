# Corey Mathie, 2026
"""
Twilio voice webhook on AWS Lambda (API Gateway HTTP API).

1. Verifies the X-Twilio-Signature header so only Twilio can trigger calls.
2. Returns TwiML that opens a media stream to the long-lived Pipecat agent,
   passing the caller's number as a custom stream parameter.
"""

from __future__ import annotations

import base64
import os
from urllib.parse import parse_qsl

from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, VoiceResponse


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


def handler(event, _context):
    form = _form(event)

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if auth_token:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        signature = headers.get("x-twilio-signature", "")
        if not RequestValidator(auth_token).validate(_public_url(event), form, signature):
            return {"statusCode": 403, "body": "invalid Twilio signature"}

    resp = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=os.environ["AGENT_PUBLIC_WS_URL"])
    stream.parameter(name="caller", value=form.get("From", "unknown"))
    resp.append(connect)
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/xml"},
        "body": str(resp),
    }
