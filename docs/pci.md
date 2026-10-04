# PCI: payment links and keypad capture

Two ways to take a payment on a call, chosen with `PAYMENT_MODE`:

| Mode | How the card is entered | Where card data goes | Default |
|---|---|---|---|
| `link` | On the processor's hosted page, from a link texted to the calling number | Stripe | yes (ADR 0004) |
| `keypad` | On the phone keypad, collected by Twilio `<Pay>` | Twilio `<Pay>` → the Pay Connector (e.g. Stripe) | no |

Both modes go through the same `take_payment` tool, so the same controls apply first: allow-list, social-engineering score, the account-takeover rule, step-up verification, per-call caps (the keypad start counts toward them), velocity, and the handler's amount limit (`MAX_PAYMENT_USD`).

This page describes the design and what the code does. It is **not** a PCI DSS assessment. This repo has not been assessed, and scope depends on the whole deployment; have a QSA review yours.

## Cardholder-data boundary (keypad mode)

```
                          ┌──────────────────── cardholder data environment (provider side) ───────────────────┐
                          │                                                                                     │
 Caller keypad ──DTMF──▶  │  Twilio <Pay>  (PCI mode on the Twilio account)  ──▶  Pay Connector ──▶ Stripe       │
                          │                                                                                     │
                          └──────────────────────────────┬──────────────────────────────────────────────────────┘
                                                         │ action callback: Result, PaymentConfirmationCode
                                                         │ (masked card fields in the callback are ignored)
                                                         ▼
 ┌─────────────────────────── this repo (intended to stay out of CHD flow) ─────────────────────────────────────┐
 │                                                                                                               │
 │  voice process (Pipecat)            keypad_payment Lambda                 twilio_pay_result Lambda            │
 │  ─ take_payment → gate, step-up,    ─ validate amount + call SID          ─ verify Twilio signature           │
 │    caps, velocity                   ─ pause recording (fail closed)       ─ read Result only                  │
 │  ─ CaptureGuard: drops DTMF always; ─ redirect call to <Say>+<Pay>        ─ resume recording if paused        │
 │    drops caller audio/transcripts     (ends the agent's media stream)     ─ <Connect><Stream> back to agent   │
 │    while a capture is active                                                with pay_result only             │
 │  ─ parks call state (resume.py)                                                                               │
 │    until the stream returns                                                                                   │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Sequence:

1. The model calls `take_payment`. With `PAYMENT_MODE=keypad`, `tools.lambda_executors("keypad")` sends it to `/keypad_payment`. `make_handler` sets `call_sid` from the call (a model-supplied one is dropped) and pins `customer_phone` as in link mode.
2. `keypad_payment` validates the amount (same function as `take_payment`) and the call SID, then pauses the call's current recording (`Recordings/Twilio.CURRENT`, status `paused`). No recording (404): continue. Any other error: raise, so no card is collected while a recording might still run, and the idempotency key is released for a retry.
3. It redirects the live call with TwiML from `pay_twiml.build_pay_twiml`: a `<Say>` explaining that the assistant can't hear the keys, then `<Pay input="dtmf" ...>`. Updating the call's TwiML ends the `<Connect><Stream>`, so the agent's media stream carries nothing while the card is entered.
4. The voice process sees `keypad_started`, sets `CaptureState` (`transcript_suppressed`, `recording_paused`), and when the stream closes it parks the call's context, outcomes, `CallPolicy`, and `StepUpSession` in `resume.SUSPENDED` instead of writing the post-call summary.
5. `<Pay>` finishes and Twilio POSTs to the action URL. `twilio_pay_result` verifies the signature, reads only `Result` and `PaymentConfirmationCode`, resumes the recording if it was paused, says the outcome, and reconnects the call to the agent with `resume=keypad_payment` and `pay_result=<Result>`.
6. The new stream picks up the parked state, so caps, verification, the risk score, and the conversation continue; the outcome becomes `keypad_paid` or `keypad_failed`, and one summary is written at the real end of the call.

## Suppression flags

| Flag | Set by | Effect | Evidence |
|---|---|---|---|
| Recording paused | `keypad_payment.pause_recording` before the redirect | No card digits are collected unless the recording is paused or there isn't one | eval `keypad-payment` (`recording_paused_before_capture`), eval `keypad-recording-pause-fails`; `tests/test_keypad.py::test_keypad_payment_fails_closed_if_the_recording_cant_be_paused`; mutation `test_evals_catch_card_capture_without_pausing_the_recording` |
| Transcript suppressed | `CaptureState.begin` when `take_payment` returns `keypad_started` | `CaptureGuard` drops caller audio and transcription frames, so nothing said during capture reaches the model, context, risk scorer, or summary | `tests/test_keypad.py::test_capture_guard_drops_keypad_tones_always_and_caller_input_during_capture` |
| Keypad tones dropped | `CaptureGuard`, always | `InputDTMFFrame`s from the Twilio media stream never reach the pipeline (nothing in this agent reads DTMF), so digits typed outside `<Pay>` don't either | same test |
| Card fields ignored | `pay_twiml.parse_pay_result` | Only `Result` and a validated `PaymentConfirmationCode` are read from the callback | `tests/test_keypad.py::test_pay_result_reads_only_the_result_fields` |

Because the media stream really ends during `<Pay>`, the guard is defense in depth; it matters if someone changes the flow to keep the stream open.

## What is real and what is simulated

- **Real code, tested here:** TwiML generation (attribute names checked against the Twilio helper library's own `<Pay>` output in `test_pay_twiml_attribute_names_match_the_twilio_helper_library`), both Lambdas' validation, ordering, and fail-closed behavior, the capture guard (driven with real Pipecat frames), and carrying call state across the hop through `bot.run_bot`.
- **Simulated:** every Twilio REST call (recording pause/resume, call redirect) uses a stand-in client in tests and evals; the browser demo builds the real TwiML and flags but redirects nothing. **Twilio `<Pay>`, a Pay Connector, and a real card capture have not been run from this repo.**
- **Not implemented:** a shared store for parked call state (it is per process; if the resumed stream lands elsewhere, the call continues fresh and the audit log records `resume_state_missing`), keypad entry for step-up codes, and `<Pay>` tokenization (`tokenType`) for saving cards.

## Deployment requirements (keypad mode)

- Twilio **PCI mode** turned on for the account, and a Pay Connector (for example Stripe) installed; set its unique name as `PAY_CONNECTOR`. Read Twilio's current `<Pay>` documentation before enabling, including how PCI mode affects logs and recordings on the account.
- `PAY_ACTION_URL` pointing at the `twilio_pay_result` route (the SAM template sets it).
- `PAYMENT_MODE=keypad` on the voice process.
- Run one voice-process instance per call path, or accept that a resumed call landing on another instance starts with fresh per-call state (velocity limits per caller still apply).

## Scope notes (both modes)

- Card data should never enter the voice system. Payment links keep entry on the processor's page; keypad mode keeps it inside Twilio `<Pay>` and the connector.
- If a caller reads a card number aloud anyway, Luhn-valid numbers are scrubbed from tool arguments before any backend sees them and never written to the audit log (`scrub_args`, eval `card-number-in-ticket`). The spoken digits were still in the call audio, speech-to-text output, and the model provider's session. Scrubbing reduces where card data persists; it does not take the voice path out of scope.
- Call recording: keep it off, or use pause/resume as above. Keypad mode pauses it automatically; link mode doesn't need to, because no card data is spoken by design.
