# Corey Mathie, 2026
"""
FastAPI server for local development: the Twilio voice webhook plus the media
stream WebSocket in one process.

Run from the repo root:
    python -m src.agent.server
Then `ngrok http 8765` and point your Twilio number's Voice webhook at
    POST  https://<ngrok-subdomain>.ngrok.app/voice

In production the /voice webhook runs on AWS Lambda (src/handlers/twilio_voice_hook.py)
and this process only serves /stream.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pipecat.runner.utils import parse_telephony_websocket
from twilio.request_validator import RequestValidator

from src.handlers.call_start import CallStartConfig, build_consent_twiml, build_voice_twiml
from src.safeguards.policy_config import active_policy

from .bot import run_bot, velocity_store

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("voice-agent")

# Fail closed: an invalid policy file (config/policy.yaml or POLICY_FILE) raises PolicyConfigError here,
# so the process never starts serving calls with a policy nobody reviewed.
POLICY = active_policy()
log.info("policy loaded from %s (sha256 %s)", POLICY.source, POLICY.version_ref)
# Same for the velocity store: a misconfigured VELOCITY_BACKEND stops startup instead of falling back to memory.
log.info("velocity store: %s", type(velocity_store()).__name__)

app = FastAPI(title="voice-agent-starter")
PUBLIC_WS_URL = os.environ.get("AGENT_PUBLIC_WS_URL", "wss://localhost:8765/stream")


def build_twiml(caller: str, form: dict | None = None, consent_url: str = "") -> str:
    """Disclosure (and consent prompt if recording needs it), then a media stream with the caller's number."""
    return build_voice_twiml(PUBLIC_WS_URL, consent_url, {**(form or {}), "From": caller}, CallStartConfig.from_env())


async def _verified_form(request: Request) -> dict:
    form = {k: str(v) for k, v in (await request.form()).items()}
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if auth_token:  # verify the request really came from Twilio
        signature = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(auth_token).validate(str(request.url), form, signature):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/voice")
async def voice_webhook(request: Request) -> Response:
    form = await _verified_form(request)
    consent_url = os.environ.get("CONSENT_ACTION_URL") or str(request.url_for("consent_webhook"))
    return Response(content=build_twiml(form.get("From", "unknown"), form, consent_url), media_type="application/xml")


@app.post("/consent")
async def consent_webhook(request: Request) -> Response:
    """Recording-consent <Gather> action (see src/handlers/call_start.py)."""
    form = await _verified_form(request)
    twiml, _ = build_consent_twiml(PUBLIC_WS_URL, form)
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    _, call_data = await parse_telephony_websocket(websocket)
    stream_sid = call_data["stream_id"]
    call_sid = call_data["call_id"]
    params = call_data.get("body") or {}
    caller = params.get("caller", "unknown")
    log.info("call started call_sid=%s resume=%s", call_sid, params.get("resume"))
    # After Twilio <Pay> the call comes back on a new stream with resume/pay_result parameters (docs/pci.md).
    await run_bot(
        websocket,
        stream_sid,
        call_sid,
        caller,
        resume=params.get("resume"),
        pay_result=params.get("pay_result"),
        stream_params=params,
    )
    log.info("call ended call_sid=%s", call_sid)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("AGENT_PORT", "8765")),
    )
