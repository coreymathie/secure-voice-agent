# Compliance notes

The patterns here come from two places: client voice-agent work in healthcare, legal, and real estate, and fraud and dispute operations in fintech. In both, a system that leaks a caller's SSN to a public LLM, or logs a card number in plaintext, is a serious incident.

The `src/safeguards/` module is the compact version of what those deployments actually need. This file explains how to use it, what it's good for, and what it isn't.

## Policy gate (`policy_gate.py`)

The first check on every tool call, before velocity. Its configuration is the reviewed policy file `config/policy.yaml` ([`policy.md`](policy.md)). The parts, all deterministic:

- **Default-deny allow-list.** Each tool has a declared risk tier (`log_lead`, `send_verification_code`, `verify_caller` low; `create_ticket`, `book_meeting` medium; `take_payment`, `update_contact` high). A tool without one is denied, including names the model invents. `POLICY_ALLOWED_TOOLS` can switch tools off for a deployment but can't add one.
- **Social-engineering score.** The caller's finalized turns are scored for urgency (1), authority claims (2), requests to skip checks (3), redirecting payments or contact details (3), and secrecy (2). Each signal counts once per turn and the score accumulates over the call. At `POLICY_RISK_THRESHOLD` (default 4), tools at or above `POLICY_HANDOFF_MIN_TIER` (default high) return `require_human`. One urgent sentence ("my basement is flooding") stays below the threshold.
- **Per-call caps.** Payment links (default 4), cumulative USD across links once one has been issued (default 5,000; the first link is capped by the handler's `MAX_PAYMENT_USD`), and completed state-changing actions (default 8). Exceeding one returns `require_human`.

- **Step-up verification and the takeover rule.** High-tier tools need a call verified by a one-time code sent to the phone on file; money movement after a contact change on the same call hands off. See [`auth.md`](auth.md).

Decisions are audited with a reason code, the risk score, and the signal names, never the caller's words. The model gets guidance it can say ("a team member will follow up") without thresholds or flagged phrases. Mapping to frameworks: [`controls.md`](controls.md); threats and residual risk: [`threat-model.md`](threat-model.md).

## PII redaction (`pii_redactor.py`)

`make_handler()` in `src/agent/tools.py` already runs `redact()` on every tool call's arguments before they reach the audit log. If you add transcript logging or any other log sink, run `redact()` on it too. The redactor returns a count-by-rule along with the scrubbed string, which is useful for two things:

1. Flagging a call for review if the counts are unusually high ("the caller said 3 SSNs" is almost certainly a confused-agent transcription error that still shouldn't survive in your logs)
2. Attaching a redaction summary to the audit entry so a reviewer can see *that* PII was present without seeing *what* it was

The PAN rule is Luhn-validated so random 16-digit strings (order numbers, invoice IDs) don't get mangled.

The audit log isn't the only place a spoken card number can land. Callers read them into whatever the agent is filling in, such as a ticket body or lead notes. Before any tool runs, `scrub_args()` removes card numbers, SSNs, bank details, DOBs, and license numbers from every string argument, so Zendesk, the CRM, and the calendar never receive them. Emails and phone numbers are kept because the tools need them. The `pii_removed_before_send` field on each `tool_call` audit entry records what was removed.

### What it's not

Not a formal DLP. For regulated workloads with real exposure, pair with:
- A commercial DLP at the egress boundary (Nightfall, Microsoft Purview, AWS Macie)
- Prompt / response policy enforcement (Lakera, Protect AI)
- Human review of a sample of calls

## Hash-chained audit log (`audit_log.py`)

Every state-changing event (tool call, provider swap, caller hangup, velocity flag) should go through `AuditLog.append`. The chain property means that any later tamper of a line is detectable by `verify_chain()`.

For true WORM semantics, forward each entry to one of:
- **AWS**: S3 Object Lock in Compliance mode
- **Azure**: immutable Blob storage policy
- **GCS**: Bucket Lock retention policy

The local JSONL file is the operational copy; the WORM store is the one you point at during an audit.

## Velocity checks (`velocity.py`)

Patterns applied from dispute/fraud operations: when a single caller produces an unusual burst of state-changing requests, that's a strong signal that something is wrong — either a fraud attempt, a confused agent, or a looping client.

Default rules:
- `take_payment`: 1 per 2 minutes, hard denied; 3 per hour, requires a human handoff
- `book_meeting`: 3 per 3 minutes, denied
- `create_ticket`, `log_lead`: 5 per 10 minutes each, denied

`make_handler()` calls `VelocityStore.check(caller_id, tool, request_id)` before every tool runs. On deny, the agent gets a polite refusal and the audit log gets a `velocity_denied` entry. On `require_human`, the agent is told to offer a callback and the log gets `handoff_required`.

Where the counts live: in process memory by default. With several voice-process instances, set `VELOCITY_BACKEND=redis` (`REDIS_URL`) so the windows are shared; the Redis store keys callers by salted hash, so no phone numbers are stored there, and it denies requests if Redis is unreachable.

What counts: every distinct request, including denied ones, so a caller who keeps pushing stays blocked. A replay of the same tool-call id counts once. A request that had no effect (rejected by validation, or failed because a provider was down) is released, so a caller who corrects an amount or retries after an outage isn't treated as abusive.

## Caller pseudonymization

The caller's phone number is needed at runtime (velocity limits, texting a payment link) but is never written to the audit log. Entries carry `caller_ref`, a salted SHA-256 of the number. Set `AUDIT_SALT` to a long random value and keep it in your secrets store; with the salt, an investigator can confirm whether a given number made a given call.

## Payments

The agent never takes card numbers by voice. `take_payment` creates a Stripe Payment Link and texts it to the caller's number, capped by `MAX_PAYMENT_USD`. This keeps PAN data out of call audio, transcripts, and LLM provider logs.

The destination number is not the model's to choose. `make_handler()` discards any `customer_phone` argument and uses the number Twilio reported for the call, so a caller can't talk the agent into texting a payment link to a third party. If the text fails after the link is created, the handler reports `sms_failed` instead of raising, so a retry can't create a second link.

## AI disclosure and recording consent

Every call opens with a fixed disclosure that the caller is talking to an AI, played by Twilio from the webhook's TwiML before the agent connects (`src/handlers/call_start.py`). It doesn't depend on the model, and the system prompt also tells the agent never to claim to be a person. Set the wording with `AI_DISCLOSURE_TEXT`.

This repo never records calls unless `RECORDING_ENABLED=true`. When recording is on:

| `RECORDING_CONSENT_MODE` | Behavior |
|---|---|
| `always` (default) | Every caller hears "press 1 to allow recording, or 2 to continue without". Only 1 is consent; another key or silence means no recording. |
| `by_jurisdiction` | Callers whose number is in an all-party state (`RECORDING_ALL_PARTY_STATES`; unknown counts as all-party), or everyone if `BUSINESS_JURISDICTION` is all-party, get the prompt. Others hear "This call may be recorded." and are recorded. |
| `off` | No recording. |

The consent result travels to the voice process as a stream parameter. There, `src/agent/disclosure.py` re-derives the rule from its own configuration (it doesn't trust the parameter alone), starts a Twilio recording only if allowed, and writes one `call_disclosure` audit entry: disclosure given or missing, consent result, jurisdiction, and whether recording started, was refused, was disabled, or failed. Set the `RECORDING_*` variables to the same values on the webhook Lambdas and the voice process; a mismatch errs toward not recording.

Limits: the jurisdiction is Twilio's `FromState`, derived from the caller's number rather than their location. The default all-party list is the commonly cited one and is conservative (it includes states where the rule is disputed or differs by call type); it is not legal advice. Recording consent is collected by keypad only. Evidence: evals `ai-disclosure-always`, `consent-declined-no-recording`, `consent-no-input-no-recording`, `consent-granted-recording`, `one-party-state-notice`; `tests/test_call_start.py`.

## Post-call summaries

Summaries are written by a third-party model, so the transcript is passed through `redact()` first (card numbers, SSNs, emails, phone numbers, and the rest are gone before it leaves). The `call_summary` Lambda scrubs card numbers and SSNs again before writing the CRM note. The audit log's `call_summary` entry carries the redacted summary and the salted caller reference, not the number. Set `CALL_SUMMARY_ENABLED=false` for deployments where no call content may leave the voice process, or `SUMMARY_PROVIDER=none` to keep only the rule-based summary built from tool outcomes.

## Evidence that the controls work

`python -m evals.run` replays the call scenarios through the real safeguard layer and handlers and checks the results: limits enforced, no identifiers reaching outside services, no internal error details shown to the model, the audit chain intact. CI runs them on every push and publishes the scorecard. For an audit, the scorecard plus `evals/scenarios.yaml` documents which risks are tested and how.

## Deployment checklist

Minimum viable regulated deployment:

- [ ] No transcript logging in production unless legal has approved it, and then only through `redact()`
- [ ] `AUDIT_SALT` set to a long random value and stored in Secrets Manager
- [ ] `TWILIO_AUTH_TOKEN` set everywhere so webhook signatures are verified
- [ ] Audit log forwarded to a WORM-backed store with ≥7-year retention
- [ ] `config/policy.yaml` (tiers, caps, risk signals, step-up, velocity) reviewed with the business — defaults are a starting point; changes go through review and `python -m evals.run --policy` ([`policy.md`](policy.md))
- [x] Tool API authenticated: HMAC-signed requests with a shared secret (`TOOL_API_SECRET`), verified before any work. Rotate the secret like any credential; IAM/SigV4 is the next step up.
- [x] AI disclosure played before the agent speaks, and recording consent where required (`call_start.py`); review `AI_DISCLOSURE_TEXT`, `RECORDING_*`, and the all-party list with counsel
- [ ] TLS terminated at the load balancer; agent process on private subnet
- [ ] Secrets in AWS Secrets Manager / Azure Key Vault, not `.env`
- [ ] Separate OpenAI/Anthropic organization for the regulated workload
- [ ] Zero data retention agreement with the LLM provider if available (Anthropic ZDR, OpenAI Zero Retention)

## Who should read this

Anyone deploying a voice agent into a regulated industry. The code in `safeguards/` is deliberately small; the hard work is in the operational wrap around it.
