# Corey Mathie, 2026
"""
twilio_consent Lambda: the recording-consent <Gather> action URL.

Twilio posts the key the caller pressed (Digits) after hearing the consent
prompt (call_start.py). 1 means consent; any other key, or silence, means no
recording. The response connects the call to the agent with the result as a
stream parameter; the voice process audits it and starts a recording only if
recording_allowed() agrees (src/agent/disclosure.py).
"""

from __future__ import annotations

import os

try:  # package import (tests, local dev)
    from .call_start import build_consent_twiml
    from .twilio_voice_hook import verified_form
except ImportError:  # Lambda imports handlers as top-level modules
    from call_start import build_consent_twiml
    from twilio_voice_hook import verified_form


def handler(event, _context):
    form = verified_form(event)
    if form is None:
        return {"statusCode": 403, "body": "invalid Twilio signature"}
    twiml, _consent = build_consent_twiml(os.environ["AGENT_PUBLIC_WS_URL"], form)
    return {"statusCode": 200, "headers": {"content-type": "application/xml"}, "body": twiml}
