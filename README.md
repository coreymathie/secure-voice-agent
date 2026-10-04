# voice-agent-starter

[![ci](https://github.com/coreymathie/voice-agent-starter/actions/workflows/ci.yml/badge.svg)](https://github.com/coreymathie/voice-agent-starter/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![pipecat](https://img.shields.io/badge/pipecat-1.x-purple)
![license](https://img.shields.io/badge/license-MIT-green)

A starter for a **voice AI agent that answers a real phone number** and takes action before it hangs up: booking a meeting, texting a payment link, opening a support ticket, or saving a lead.

What makes it different from most voice-agent demos is the part that runs between the LLM and your business systems. Every tool call passes through **velocity checks, PII scrubbing, a hash-chained audit log, and idempotency**. Those are the patterns I worked with in fraud and dispute operations at Capital One and Fundbox, applied to an AI agent that can move money and touch customer data.

Two more things a production team asks for before an agent answers real calls:

- **Call evals.** 13 scripted call scenarios (payment bursts, replayed requests, provider outages, a card number read into a ticket, a prompt-injected attempt to text a payment link elsewhere) run through the real tool stack in CI and publish a scorecard. Mutation tests confirm the evals fail when a safeguard is removed.
- **Post-call summaries.** When a call ends, the conversation becomes a structured CRM note (intent, outcome, follow-ups, every tool result), written from a PII-redacted transcript.

**Stack:** Pipecat 1.x · Twilio Media Streams · OpenAI Realtime (or Claude + Deepgram + ElevenLabs, or Gemini Live) · AWS Lambda + API Gateway + DynamoDB · Fly.io

---

## Architecture

```
  Caller ──PSTN──▶ Twilio ──POST /voice──▶ Lambda: twilio_voice_hook
                     │                      (verifies Twilio signature,
                     │                       returns <Stream> TwiML with the caller's number)
                     │
                     └──wss /stream──▶ Pipecat voice process (Fly.io / ECS)
                                         │  transport ▶ VAD ▶ LLM ▶ transport
                                         │
                                         │  tool call
                                         ▼
                              ┌─────────────────────────────┐
                              │ Safeguard layer (per caller)│
                              │  1. velocity check          │
                              │  2. PII-redacted audit entry│
                              │  3. idempotency key         │
                              └──────────────┬──────────────┘
                                             │ HTTPS
                                             ▼
                        Lambda handlers (claim key in DynamoDB, release on failure)
                          book_meeting ▶ Google Calendar
                          take_payment ▶ Stripe Payment Link, texted by Twilio SMS
                          create_ticket ▶ Zendesk
                          log_lead ▶ GoHighLevel
                          call_summary ▶ GoHighLevel contact note (after hang-up)
```

**Why two deploy targets?** A phone call is a long-lived WebSocket, often 2 to 8 minutes, so it doesn't fit Lambda's execution model or cold-start latency. The **stateless** parts (the webhook and the four tool handlers) run on Lambda: fast, cheap, scale to zero. The **stateful** voice process runs on Fly.io or ECS.

---

## The safeguard layer

All of it lives in `src/safeguards/` and is applied to every tool by `make_handler()` in `src/agent/tools.py`.

| Control | What it does | Why |
|---|---|---|
| **Velocity checks** | Per-caller sliding windows per tool. Example: 1 payment link per 2 minutes (denied beyond that), more than 3 per hour hands off to a human. | Fraud attempts and confused agents both show up as bursts of state-changing actions. |
| **PII redaction** | Scrubs SSNs, Luhn-valid card numbers, DOBs, bank routing/account numbers, emails, phones, and license numbers from everything written to the audit log. | Logs outlive calls. They shouldn't hold data you'd have to disclose in a breach. |
| **Identifier scrubbing before send** | Card numbers, SSNs, bank details, DOBs, and license numbers are removed from tool arguments *before* they reach Zendesk, the CRM, or the calendar, even inside free text like a ticket body. | Callers read card numbers aloud. They shouldn't end up in your ticketing system. |
| **Payment links go to the caller** | The SMS always goes to the number that is calling; a `customer_phone` supplied by the model is ignored. | A caller can't talk the agent into texting a payment link to someone else. |
| **Hash-chained audit log** | Every tool call, result, denial, and error is appended to a JSONL log where each entry carries the SHA-256 of the previous one. `verify_chain()` finds the first tampered line. | Gives a reviewer an audit trail they can trust, not just a text file. |
| **Caller pseudonymization** | The caller's phone number is stored only as a salted hash. | Lets you trace a caller's activity without keeping the raw number in logs. |
| **Idempotency** | The LLM's tool-call id becomes the idempotency key. Lambda claims it with a conditional DynamoDB write and releases it if the call fails. A replayed id isn't counted twice by the velocity limits. | Prevents double bookings and double charges from replays and retries, without blocking a legitimate retry after a failure. |
| **Honest failure handling** | A rejected request (over the payment limit, a bad timestamp) returns the reason so the agent can explain it. An outage returns a generic message, keeps details in the audit log, and doesn't use up the caller's velocity allowance. | The caller hears "the limit for phone payments is $5,000" instead of "try again later", and a retry after our outage isn't treated as abuse. |
| **No card numbers by voice** | Payments go out as a Stripe Payment Link by SMS, with a configurable max amount. | Keeps card data out of transcripts, recordings, and LLM provider logs (and out of PCI scope). |

More detail: [`docs/compliance.md`](docs/compliance.md).

---

## Quickstart (local)

```bash
git clone https://github.com/coreymathie/voice-agent-starter.git
cd voice-agent-starter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add OPENAI_API_KEY and your Twilio credentials

python -m src.agent.server        # serves /voice and /stream on :8765
ngrok http 8765                   # in a second terminal
```

Set `AGENT_PUBLIC_WS_URL=wss://<your-ngrok-host>/stream` in `.env` and restart. In the Twilio console, set your number's **A call comes in** webhook to `POST https://<your-ngrok-host>/voice`. Call the number.

Full walkthrough: [`docs/setup.md`](docs/setup.md). Deploying to AWS and Fly.io: [`docs/deploy.md`](docs/deploy.md).

## Call evals

```bash
python -m evals.run
```

```
**13/13 scenarios passed**

|    | Scenario                                             | Tool outcomes                                     |
|----|------------------------------------------------------|---------------------------------------------------|
| ✅ | Three payment links in under a minute                 | link_sent → denied → denied                        |
| ✅ | Fourth payment request within an hour                 | link_sent → link_sent → link_sent → require_human  |
| ✅ | The same tool call arrives twice                      | link_sent → duplicate                              |
| ✅ | Stripe is down, then the caller tries again           | error → link_sent                                  |
| ✅ | Amount over the phone limit, then a corrected amount  | rejected → link_sent                               |
| ✅ | Caller reads a card number and SSN into a support ticket | created (Zendesk never receives either)         |
| ✅ | Model is talked into texting the link elsewhere       | link_sent (to the caller's own number)             |
| ...                                                                                                                   |
```

Each scenario in [`evals/scenarios.yaml`](evals/scenarios.yaml) is the sequence of tool calls a model makes during one call, with timing. The harness runs them through the real stack: `make_handler`, velocity limits, PII scrubbing, the audit log, and the actual Lambda handler code with its idempotency layer. Only Stripe, Twilio SMS, Google Calendar, Zendesk, and the CRM are faked, and the fakes enforce the real services' rules (Google Calendar rejects a time with no time zone). A fake clock drives the velocity windows, so an hour-long call runs in milliseconds.

After the steps, each scenario checks what the outside world received (how many payment links, where each text went, whether a card number reached Zendesk), what the model was shown (no stack traces or internal hostnames), and that the audit log's hash chain is intact.

In CI the scorecard is written to the GitHub Actions job summary, and `tests/test_evals.py` switches off one safeguard at a time to confirm the evals catch it. Writing these scenarios exposed seven defects in 0.3.0, all fixed in 0.4.0 (see the [changelog](CHANGELOG.md)).

Adding a scenario is a few lines of YAML; no Python needed.

## Post-call summaries

When the caller hangs up, `src/agent/summary.py` turns the call into a CRM note:

```
Voice agent call · Needs follow-up
Intent: roof estimate · Sentiment: positive

Dana asked for a roof estimate and was booked for Tuesday at 2pm. She also asked
for pricing on gutter guards, which the agent couldn't answer.

Follow-ups:
- Email gutter guard pricing before Tuesday's visit
Actions: book_meeting → booked
Call SID: CA3f…
```

- The transcript is PII-redacted before it goes to the summarizing model, and the tool outcomes are passed as facts, so the note never claims a booking or payment that didn't happen.
- Claude (forced tool call) or OpenAI (strict JSON schema), chosen by `SUMMARY_PROVIDER` or whichever key is set. With neither, or if the model call fails, a rule-based summary is written instead, so every call leaves a record.
- The summary is written to the audit log and sent to the `call_summary` Lambda, which finds or creates the caller's GoHighLevel contact by phone number and adds the note. Delivery is idempotent on the call SID.
- It runs even if the call ends abruptly (dropped connection), and it can never crash call teardown.

## Tests

```bash
ruff check src tests evals && pytest -q
```

55 tests cover the safeguard layer, the Lambda handlers (signature validation, idempotency claim/release, payment limits, SMS failure, the CRM note), the local webhook, the Pipecat wiring (every provider's services build, every tool goes through the safeguard layer, a crashed call is still summarized), both summary backends and their fallbacks, and the evals themselves.

---

## Swapping providers

```bash
AGENT_PROVIDER=openai_realtime    # default, speech-to-speech
AGENT_PROVIDER=anthropic_11labs   # Deepgram STT -> Claude -> ElevenLabs TTS
AGENT_PROVIDER=gemini_live        # Gemini Live, speech-to-speech
```

Each provider is a small class in `src/agent/provider.py` that returns the Pipecat services for its slot. The pipeline, tools, and safeguards don't change.

---

## Layout

```
src/
  agent/        Pipecat process: server.py (webhook + stream), bot.py (pipeline),
                provider.py (LLM/voice services), tools.py (tool specs + safeguarded handlers),
                summary.py (post-call summaries)
  safeguards/   pii_redactor.py, audit_log.py, velocity.py
  handlers/     Lambda functions + shared idempotency helpers
evals/          scenarios.yaml, harness.py, run.py (call evals and scorecard)
infra/          AWS SAM template
Dockerfile, fly.toml  voice-process image and Fly.io config
docs/           setup, deploy, architecture, compliance
tests/          pytest suite
```

## Limitations

- Velocity state is in memory, per process. Running more than one agent instance needs a shared store (Redis) so limits apply across instances.
- No outbound calling yet. That needs `twilio.rest.Client.calls.create(...)` plus a scheduler.
- PII redaction is pattern-based. It closes the common gaps but is not a substitute for a formal DLP in high-exposure deployments.
- No memory across calls. A returning caller starts fresh (the CRM notes are there for staff, not yet fed back to the agent).
- The evals exercise the tool layer with scripted tool calls. They don't yet drive a live model with simulated caller speech; that needs API keys in CI and is the natural next step.

## License

MIT. See `LICENSE`.
