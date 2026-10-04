# Corey Mathie, 2026
"""
Voice-process side of the call-start disclosure and recording consent
(src/handlers/call_start.py plays them in TwiML before the stream connects).

When a call's media stream starts, on_call_start():
  1. reads the stream parameters the TwiML set (disclosure, consent, jurisdiction),
  2. decides whether recording is allowed with call_start.recording_allowed(),
     re-derived from this process's configuration rather than trusted from the
     parameters,
  3. starts a Twilio call recording only if it is allowed and recording is
     enabled, and
  4. appends one `call_disclosure` audit entry: disclosure given or missing, the
     consent result, the jurisdiction used, and what happened to recording
     (started, refused_no_consent, disabled, or failed).

A call that arrives without `disclosure=given` (for example a Twilio number
pointed straight at /stream, skipping the webhook) is audited as
`disclosure: missing` and never recorded.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from src.handlers.call_start import CONSENT_RESULTS, CallStartConfig, recording_allowed


def _twilio_client():
    from twilio.rest import Client

    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def decide(params: Mapping[str, Any], cfg: CallStartConfig) -> dict:
    """Audit fields and whether to record, from the stream parameters. Pure: no I/O."""
    disclosure = "given" if params.get("disclosure") == "given" else "missing"
    consent = str(params.get("consent") or "missing")
    consent = consent if consent in CONSENT_RESULTS else "missing"
    jurisdiction = str(params.get("jurisdiction") or "unknown")[:16]
    if not cfg.recording_enabled or cfg.consent_mode == "off":
        recording = "disabled"
    elif disclosure == "given" and recording_allowed(consent, jurisdiction, cfg):
        recording = "allowed"
    else:
        recording = "refused_no_consent"
    return {"disclosure": disclosure, "consent": consent, "jurisdiction": jurisdiction, "recording": recording}


def on_call_start(
    params: Mapping[str, Any],
    call_sid: str | None,
    caller_ref: str,
    audit,
    *,
    cfg: CallStartConfig | None = None,
    client_factory: Callable[[], Any] = _twilio_client,
) -> dict:
    """Start a recording if (and only if) allowed, then audit the call-start outcome. Never raises."""
    cfg = cfg or CallStartConfig.from_env()
    fields = decide(params, cfg)
    if fields["recording"] == "allowed":
        try:
            client_factory().calls(call_sid).recordings.create()
            fields["recording"] = "started"
        except Exception as e:  # noqa: BLE001 - a recording failure must not drop the call
            fields["recording"] = "failed"
            fields["error"] = str(e)[:200]
    audit.append("call_disclosure", {"caller_ref": caller_ref, "call_sid": call_sid, **fields})
    return fields
