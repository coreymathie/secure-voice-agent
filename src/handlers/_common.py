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
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

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


def idempotency_key(event: dict[str, Any]) -> str | None:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("idempotency-key")


def idempotent(fn: Callable[[dict, Any], dict]) -> Callable[[dict, Any], dict]:
    """Wrap a handler with claim-on-entry, release-on-failure idempotency."""

    @functools.wraps(fn)
    def wrapper(event, context):
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
