# ADR 0004: Payments by link texted to the calling number, not card capture by voice or keypad

- Status: accepted. Keypad capture was added in 0.6.0 as an opt-in alternative (`PAYMENT_MODE=keypad`, `docs/pci.md`); links remain the default.
- Date: 2026-10
- Code: `src/handlers/take_payment.py`, `make_handler()` in `src/agent/tools.py`

## Context

A caller wants to pay during the call. Options:

1. **Read the card number aloud to the agent.** The PAN lands in call audio, the speech-to-text output, the LLM provider's logs, and anything else that sees the transcript. Every one of those systems is then in PCI DSS scope.
2. **Keypad (DTMF) capture.** The caller types the card on the keypad. Done properly (a PCI-compliant capture service that masks the tones and keeps them out of the media stream to the agent), the agent never sees the digits. It needs a provider feature and careful call-flow design.
3. **A hosted payment link sent by SMS.** The card is entered on the payment processor's page. The agent only knows that a link was created.

## Decision

- `take_payment` creates a Stripe Payment Link and texts it to the caller.
- The destination is not the model's to choose. `make_handler()` discards any model-supplied `customer_phone` and sets it to the number Twilio reported for the call, before any other check runs. Eval `payment-link-redirect` covers it.
- The handler caps a single link at `MAX_PAYMENT_USD` (default 5,000) and rejects non-numeric, non-finite, and non-positive amounts. The policy gate adds per-call limits on top (number of links, cumulative USD).
- If the SMS fails after the link exists, the handler returns `sms_failed` and keeps its idempotency claim, so a retry can't create a second link.
- The link URL isn't returned to the model when it was texted, so the agent can't read it aloud.
- Card numbers that callers read aloud anyway are scrubbed (Luhn-validated) from tool arguments before they leave the agent, and never written to the audit log.

## Consequences

- Positive: card data stays with the payment processor. The voice system's PCI exposure is reduced to not *accidentally* receiving card data (see the PCI notes in `docs/controls.md`). This is scope reduction, not a compliance certification.
- Positive: binding the link to the calling number defeats "text it to my assistant instead" at the code level, independent of the model.
- Negative: caller ID can be spoofed. A spoofed number receives nothing useful (the link goes to the real owner of that number), but the binding is not authentication. Since 0.6.0, `take_payment` also requires step-up verification (a one-time code to the phone on file; `docs/auth.md`).
- Negative: the caller needs a phone that can receive SMS and open a link. Keypad capture through Twilio `<Pay>` is the alternative (0.6.0, `docs/pci.md`).
- Negative: anyone who can call the `take_payment` Lambda directly can choose `customer_phone`; see ADR 0003 on endpoint authentication.
