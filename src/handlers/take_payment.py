# Corey Mathie, 2026
"""
take_payment Lambda: creates a one-time Stripe Payment Link and texts it to the
caller with Twilio SMS.

Card numbers are never collected over the phone. Reading digits aloud to a
voice agent puts PAN data in transcripts, recordings, and LLM provider logs,
which drags the whole system into PCI scope. A hosted payment link keeps the
card data with Stripe.
"""

from __future__ import annotations

import logging
import math
import os

try:  # package import (tests, local dev)
    from ._common import bad, idempotent, ok, payload_of
except ImportError:  # Lambda imports handlers as top-level modules
    from _common import bad, idempotent, ok, payload_of

MAX_AMOUNT_USD = float(os.environ.get("MAX_PAYMENT_USD", "5000"))
log = logging.getLogger(__name__)


def _send_sms(to: str, text: str) -> bool:
    sid, token, sender = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"))
    if not (sid and token and sender and to and to != "unknown"):
        return False
    from twilio.rest import Client

    Client(sid, token).messages.create(to=to, from_=sender, body=text)
    return True


def validate_amount(body: dict, limit: float | None = None) -> tuple[float | None, str | None]:
    """(amount, None) or (None, message for the caller). Shared with the keypad payment handler."""
    limit = MAX_AMOUNT_USD if limit is None else limit
    try:
        amount = float(body["amount_usd"])
    except (KeyError, TypeError, ValueError):
        return None, "amount_usd must be a number"
    if not math.isfinite(amount):  # float("nan") and float("inf") parse, and NaN slips past both comparisons
        return None, "amount_usd must be a number"
    if amount <= 0:
        return None, "amount_usd must be positive"
    if amount > limit:
        return None, f"amount exceeds the ${limit:,.0f} limit for phone payments"
    return amount, None


@idempotent
def handler(event, _context):
    body = payload_of(event)
    for field in ("amount_usd", "description", "customer_email"):
        if field not in body:
            return bad(f"missing field: {field}")

    amount, problem = validate_amount(body)
    if problem:
        return bad(problem)

    import stripe

    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    product = stripe.Product.create(name=body["description"])
    price = stripe.Price.create(product=product.id, currency="usd", unit_amount=round(amount * 100))
    link = stripe.PaymentLink.create(
        line_items=[{"price": price.id, "quantity": 1}],
        after_completion={"type": "hosted_confirmation"},
        metadata={"customer_email": body["customer_email"], "source": "voice-agent"},
    )

    phone = body.get("customer_phone", "")
    try:
        texted = _send_sms(phone, f"Your secure payment link for {body['description']} (${amount:,.2f}): {link.url}")
    except Exception as e:  # noqa: BLE001 - the link exists; report the SMS failure instead of failing the call
        # Raising here would release the idempotency key, and a retry would create a second link.
        log.error("payment link %s created but SMS failed: %s", link.id, e)
        return ok(
            {
                "status": "sms_failed",
                "message": "The payment link was created but the text message didn't send. "
                "Offer to have someone from the team send it.",
            }
        )
    # Don't hand the URL back to the LLM when it was texted; the agent would read it aloud.
    if texted:
        return ok({"status": "link_sent", "channel": "sms"})
    return ok({"status": "link_created", "channel": "none", "url": link.url})
