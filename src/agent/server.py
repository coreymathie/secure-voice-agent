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
from twilio.twiml.voice_response import Connect, VoiceResponse

from .bot import run_bot

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("voice-agent")

app = FastAPI(title="voice-agent-starter")
PUBLIC_WS_URL = os.environ.get("AGENT_PUBLIC_WS_URL", "wss://localhost:8765/stream")


def build_twiml(caller: str) -> str:
    """TwiML that opens a media stream and passes the caller's number as a custom parameter."""
    resp = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=PUBLIC_WS_URL)
    stream.parameter(name="caller", value=caller)
    resp.append(connect)
    return str(resp)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/voice")
async def voice_webhook(request: Request) -> Response:
    form = dict(await request.form())
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if auth_token:  # verify the request really came from Twilio
        signature = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(auth_token).validate(str(request.url), form, signature):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return Response(content=build_twiml(form.get("From", "unknown")), media_type="application/xml")


@app.websocket("/stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    _, call_data = await parse_telephony_websocket(websocket)
    stream_sid = call_data["stream_id"]
    call_sid = call_data["call_id"]
    caller = (call_data.get("body") or {}).get("caller", "unknown")
    log.info("call started call_sid=%s", call_sid)
    await run_bot(websocket, stream_sid, call_sid, caller)
    log.info("call ended call_sid=%s", call_sid)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("AGENT_PORT", "8765")),
    )
