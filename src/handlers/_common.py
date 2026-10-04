# Corey Mathie, 2026
"""
Shared helpers for the Lambda handlers: idempotency, request parsing, responses.

Idempotency model
-----------------
Each tool call carries an Idempotency-Key (the LLM's tool_call_id). The first
request to arrive *claims* the key with a conditional DynamoDB write, which is
race-safe across concurrent Lambda invocations. If the handler then fails, the
claim is released so a legitimate retry can run. A successful call keeps the
claim for 24 hours, so replays return "duplicate" instead of double-booking or
double-charging.

Request signing
---------------
Tool routes are public HTTPS endpoints, so a request is only trusted if it is
signed by the voice process. The signature is HMAC-SHA256 over
`timestamp.idempotency_key.body` with a shared secret (`TOOL_API_SECRET`):
it binds the body (no tampering), the idempotency key (a captured request
can't be replayed under a new key) and the time (requests older than
`MAX_SKEW_SECONDS` are refused). Without a secret, handlers refuse every call
unless `ALLOW_UNSIGNED_TOOL_CALLS=true` is set for local development.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

MAX_SKEW_SECONDS = 300
SIGNATURE_HEADER = "X-Tool-Signature"
TIMESTAMP_HEADER = "X-Tool-Timestamp"

log = logging.getLogger(__name__)

_TABLE = None
_TABLE_NAME = os.environ.get("IDEMPOTENCY_TABLE", "voice-agent-idempotency")
_TTL_SECONDS = 24 * 3600


def _table():
    global _TABLE
    if _TABLE is None:
        import boto3  # available in the Lambda runtime

        _TABLE = boto3.resource("dynamodb").Table(_TABLE_NAME)
    return _TABLE


def seen_before(key: str) -> bool:
    """Atomically claim `key`. Returns True if someone already claimed it."""
    try:
        _table().put_item(
            Item={"key": key, "expires_at": int(time.time()) + _TTL_SECONDS},
            ConditionExpression="attribute_not_exists(#k)",
            ExpressionAttributeNames={"#k": "key"},
        )
        return False
    except Exception as e:  # noqa: BLE001 - boto raises dynamic exception classes
        if type(e).__name__ == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in str(e):
            return True
        # Fail open so the agent keeps working if DynamoDB is degraded. Log loudly.
        log.error("idempotency store unavailable, failing open: %s", e)
        return False


def release(key: str) -> None:
    """Drop a claim after a failed call so a retry can proceed."""
    try:
        _table().delete_item(Key={"key": key})
    except Exception as e:  # noqa: BLE001 - best effort; never mask the original failure
        log.error("could not release idempotency key %s: %s", key, e)


def ok(body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def bad(message: str, status: int = 400) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    return json.loads(body) if isinstance(body, str) else body


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def idempotency_key(event: dict[str, Any]) -> str | None:
    return _headers(event).get("idempotency-key")


def sign(secret: str, timestamp: str, idempotency_key: str, body: bytes) -> str:
    """HMAC-SHA256 over `timestamp.idempotency_key.body`, hex, versioned."""
    message = timestamp.encode() + b"." + idempotency_key.encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, idempotency_key: str, body: bytes, now: float | None = None) -> dict[str, str]:
    """Headers the voice process sends with a tool request."""
    ts = str(int(now if now is not None else time.time()))
    return {
        "Idempotency-Key": idempotency_key,
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: sign(secret, ts, idempotency_key, body),
    }


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body")
    if body is None:
        return b""
    if isinstance(body, str):
        return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    return json.dumps(body, separators=(",", ":")).encode()


def verify_signature(event: dict[str, Any], now: float | None = None) -> str | None:
    """Return None if the request is authentic, else a reason (logged, never sent to the caller)."""
    secret = os.environ.get("TOOL_API_SECRET", "")
    if not secret:
        if os.environ.get("ALLOW_UNSIGNED_TOOL_CALLS", "").lower() == "true":
            return None
        return "TOOL_API_SECRET is not configured"
    headers = _headers(event)
    ts = headers.get(TIMESTAMP_HEADER.lower(), "")
    sig = headers.get(SIGNATURE_HEADER.lower(), "")
    if not ts or not sig:
        return "missing signature"
    try:
        age = abs((now if now is not None else time.time()) - int(ts))
    except ValueError:
        return "invalid timestamp"
    if age > MAX_SKEW_SECONDS:
        return "stale timestamp"
    expected = sign(secret, ts, headers.get("idempotency-key", ""), _raw_body(event))
    if not hmac.compare_digest(expected, sig):
        return "bad signature"
    return None


def idempotent(fn: Callable[[dict, Any], dict]) -> Callable[[dict, Any], dict]:
    """Wrap a handler with signature verification, then claim-on-entry, release-on-failure idempotency."""

    @functools.wraps(fn)
    def wrapper(event, context):
        problem = verify_signature(event)
        if problem:
            log.warning("rejected unsigned or invalid tool request: %s", problem)
            return bad("unauthorized", 401)
        key = idempotency_key(event)
        if key and seen_before(key):
            return ok({"status": "duplicate", "message": "already handled"})
        try:
            resp = fn(event, context)
        except Exception:
            if key:
                release(key)
            raise
        if key and resp.get("statusCode", 200) >= 400:
            release(key)
        return resp

    return wrapper
