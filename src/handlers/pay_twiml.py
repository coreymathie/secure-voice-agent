# Corey Mathie, 2026
"""
TwiML for PCI keypad capture with Twilio <Pay> (PAYMENT_MODE=keypad).

Card digits are typed on the phone keypad and collected by Twilio <Pay>, which
hands them to a payment connector (for example Stripe) configured in the Twilio
console. The voice agent never receives them: to run <Pay>, the live call is
redirected away from the <Connect><Stream> that feeds the agent, so no audio and
no keypad tones reach the agent's media stream while the card is entered. When
<Pay> finishes, Twilio requests the `action` URL (twilio_pay_result.py), which
reconnects the call to the agent with only the result code.

    agent stream ──redirect──▶ <Say> + <Pay> (Twilio + connector hold the card data)
                                   │ action URL with Result
                                   ▼
                  twilio_pay_result ──▶ <Say outcome> + <Connect><Stream> (back to the agent)

Only the fields in RESULT_FIELDS are read from Twilio's callback; the masked
card number, expiry, and tokens are ignored, so nothing card-related is passed
back to the agent or logged.

Standard library only: the Lambda handlers import it, the voice process imports
it, and the browser demo runs it under Pyodide. tests/test_keypad.py checks the
markup against the Twilio helper library's own <Pay> output.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

PAYMENT_MODES: tuple[str, ...] = ("link", "keypad")

# Twilio <Pay> Result values (action callback) and what the agent's caller hears afterwards.
RESULT_MESSAGES: dict[str, str] = {
    "success": "Thank you. Your payment went through.",
    "too-many-failed-attempts": "The card details couldn't be confirmed, so no payment was taken.",
    "payment-connector-error": "The payment didn't go through, and no payment was taken.",
    "caller-interrupted-with-star": "No problem, the payment was cancelled.",
    "validation-error": "The payment didn't go through, and no payment was taken.",
    "internal-error": "The payment didn't go through, and no payment was taken.",
    "caller-hung-up": "",
}
RESULT_FIELDS: tuple[str, ...] = ("Result", "PaymentConfirmationCode")  # everything else is ignored
_CONFIRMATION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def payment_mode(env: dict | None = None) -> str:
    """PAYMENT_MODE=link|keypad. Anything else means link (the 0.5.0 behavior)."""
    import os

    raw = ((env if env is not None else os.environ).get("PAYMENT_MODE") or "link").strip().lower()
    return raw if raw in PAYMENT_MODES else "link"


@dataclass(frozen=True)
class KeypadConfig:
    connector: str = "Default"  # the Pay Connector's unique name in the Twilio console
    currency: str = "usd"
    timeout_seconds: int = 10  # seconds of keypad silence before <Pay> re-prompts
    max_attempts: int = 2
    postal_code: bool = False
    security_code: bool = True
    language: str = "en-US"


def _attrs(pairs: list[tuple[str, str]]) -> str:
    return "".join(f" {k}={quoteattr(v)}" for k, v in pairs)


def charge_amount(amount_usd) -> str:
    """Twilio wants a decimal string. Rejects non-numeric, non-finite, and non-positive amounts."""
    try:
        value = float(amount_usd)
    except (TypeError, ValueError) as e:
        raise ValueError("amount_usd must be a number") from e
    if not math.isfinite(value) or value <= 0:
        raise ValueError("amount_usd must be a positive number")
    return f"{value:.2f}"


def build_pay_twiml(
    amount_usd,
    description: str,
    action_url: str,
    config: KeypadConfig | None = None,
    *,
    status_callback: str | None = None,
    resume_recording: bool = False,
) -> str:
    """<Response><Say/><Pay/></Response> for one keypad payment."""
    cfg = config or KeypadConfig()
    amount = charge_amount(amount_usd)
    query = urlencode({"resume_recording": "1"}) if resume_recording else ""
    action = action_url + (("&" if "?" in action_url else "?") + query if query else "")
    pairs = [
        ("input", "dtmf"),
        ("paymentConnector", cfg.connector),
        ("paymentMethod", "credit-card"),
        ("chargeAmount", amount),
        ("currency", cfg.currency),
        ("description", str(description)[:255]),
        ("action", action),
        ("timeout", str(cfg.timeout_seconds)),
        ("maxAttempts", str(cfg.max_attempts)),
        ("postalCode", "true" if cfg.postal_code else "false"),
        ("securityCode", "true" if cfg.security_code else "false"),
        ("language", cfg.language),
    ]
    if status_callback:
        pairs.append(("statusCallback", status_callback))
    intro = (
        f"You'll now enter your card on the phone keypad to pay {amount} {cfg.currency.upper()}. "
        "The assistant can't hear the keys you press."
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(intro)}</Say><Pay{_attrs(pairs)} /></Response>'
    )


@dataclass(frozen=True)
class PayOutcome:
    result: str  # one of RESULT_MESSAGES, or "unknown"
    confirmation: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result == "success"

    @property
    def message(self) -> str:
        return RESULT_MESSAGES.get(self.result, "The payment didn't go through, and no payment was taken.")


def parse_pay_result(form: dict) -> PayOutcome:
    """Read only the result fields from Twilio's <Pay> action callback; card-related fields are ignored."""
    result = str(form.get("Result") or "").strip().lower()
    if result not in RESULT_MESSAGES:
        result = "unknown"
    code = str(form.get("PaymentConfirmationCode") or "").strip()
    return PayOutcome(result, code if _CONFIRMATION.match(code) else None)


def build_resume_twiml(stream_url: str, caller: str, outcome: PayOutcome) -> str:
    """Tell the caller the outcome, then reconnect them to the agent with only the result code."""
    if outcome.result == "caller-hung-up":
        return '<?xml version="1.0" encoding="UTF-8"?><Response />'
    params = [("caller", caller), ("resume", "keypad_payment"), ("pay_result", outcome.result)]
    inner = "".join(f"<Parameter{_attrs([('name', k), ('value', v)])} />" for k, v in params)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(outcome.message)}</Say>"
        f"<Connect><Stream{_attrs([('url', stream_url)])}>{inner}</Stream></Connect></Response>"
    )
