# Threat model

This document is the threat model for one inbound phone call, from Twilio's webhook to the tool backends and the post-call summary. Every row maps a threat to the control that addresses it **in this repository's code**, the test or eval that shows the control working, and the residual risk. Where there is no control, the row says **not implemented** and the item is on the roadmap.

Test references: `tests/<file>.py::<test>`; eval references: `eval:<id>` in `evals/scenarios.yaml` (run with `python -m evals.run`). Mutation tests in `tests/test_evals.py` switch a control off and confirm the evals fail.

## System and trust boundaries

```
 Caller (untrusted speech, spoofable caller ID)
   │  PSTN
   ▼
 Twilio ──POST /voice (signed)──▶ Lambda twilio_voice_hook ──TwiML──▶ Twilio
   │  wss /stream (caller number as a stream parameter)
   ▼
 Voice process: Pipecat pipeline ── model provider (OpenAI / Anthropic+Deepgram+ElevenLabs / Google)
   │  tool call proposed by the model (untrusted)
   ▼
 make_handler(): destination pin → policy gate (incl. step-up) → velocity → scrub + audit → execute
   │                    └─ step-up tools: CRM lookup (caller ID as key) → Verifier → phone ON FILE
   │  HTTPS + Idempotency-Key + HMAC     (B3: tool API, signed requests)
   ▼
 Lambda tool handlers ──▶ Stripe · Twilio SMS · Google Calendar · Zendesk · GoHighLevel
```

| Boundary | What crosses it | Trust |
|---|---|---|
| B1 Caller → Twilio → voice process | audio, caller ID | Caller is untrusted. Caller ID is a claim, not an identity: it is only a lookup key for step-up verification. |
| B2 Voice process ↔ model provider | audio, transcripts, tool schemas, tool results | Model output (including tool calls) is untrusted input to `make_handler()`. |
| B3 Voice process → tool Lambdas | tool arguments | Arguments are scrubbed before crossing. Every request is HMAC-signed (timestamp, idempotency key, body) and verified before any work. |
| B4 Lambdas → SaaS backends | payments, messages, records | Service credentials from Secrets Manager. |
| B5 Telemetry/audit sinks | audit JSONL, optional OTLP spans | Audit entries are redacted. Pipecat spans can contain transcript text (see `docs/observability.md`). |

## STRIDE

| Threat | Example | Control in code | Evidence | Residual risk |
|---|---|---|---|---|
| **S**poofing: forged Twilio webhook | Attacker POSTs to `/voice` to start sessions | `X-Twilio-Signature` verified in `src/handlers/twilio_voice_hook.py` and `src/agent/server.py` when `TWILIO_AUTH_TOKEN` is set | `tests/test_handlers.py::test_voice_hook_rejects_bad_signature`, `tests/test_server.py::test_voice_rejects_forged_request` | Verification is skipped if the token isn't configured. The `/stream` WebSocket doesn't verify that the connection came from Twilio (not implemented). |
| **S**poofing: caller ID | Attacker spoofs a customer's number to pay or change details | Step-up verification: high-tier tools need a one-time code sent to the phone **on file** (caller ID is only a lookup key; the code never goes to a number the caller or model supplies); 3 wrong codes lock the call; payment links still go only to the calling number | `eval:caller-id-is-not-identity`, `eval:otp-lockout`, `eval:payment-link-redirect`, `tests/test_step_up.py`, mutation `tests/test_evals.py::test_evals_catch_codes_sent_to_caller_id` | SMS/voice codes are a restricted authenticator (SIM swap, porting, interception). The `RiskSignalProvider` hook exists but no carrier/vendor data source ships with the repository. See `docs/auth.md` risk acceptance. |
| **S**poofing: SIM swap / number port | Attacker moves the customer's number to their own SIM, then passes the code | `RiskSignalProvider` checked before any code is sent; `recent_sim_swap` / `recent_number_port` / `recent_contact_change` hand off without sending | `eval:sim-swap-signal`, `tests/test_step_up.py::test_sim_swap_signal_blocks_the_pstn_and_the_reason_stays_out_of_the_result` | Only as good as the signal source the deployer plugs in; the default reports nothing. |
| **T**ampering: account takeover by contact change | Verified (or socially engineered) caller changes the email on file, then moves money | `update_contact` is high tier (step-up); its `customer_id` comes from the verified session; any money-moving tool after a contact change on the same call hands off; a second change in a day hands off (velocity) | `eval:account-takeover-sequence`, `tests/test_step_up.py::test_update_contact_record_comes_from_the_verified_session` | No notification to the previous contact (**not implemented**). The rule is per call: change today, pay tomorrow isn't linked. |
| **S**poofing: direct calls to tool Lambdas | Attacker calls `/take_payment` with any `customer_phone` | HMAC-SHA256 request signing with `TOOL_API_SECRET`; 5-minute timestamp window; signature binds the idempotency key and body; fail-closed when the secret is missing | `tests/test_request_signing.py` | Shared secret, not per-caller identity: anyone holding the secret can sign. Store it in Secrets Manager, rotate it, and consider IAM/SigV4 or private networking. |
| **T**ampering: audit log edited | Insider changes a `tool_result` to hide an action | Hash-chained JSONL (`src/safeguards/audit_log.py`); `verify_chain()` returns the first bad line | `tests/test_safeguards.py::test_audit_chain_detects_tampering`; every eval verifies the chain; the console's tamper action | Someone with write access can rewrite the file from the edited line onward and recompute every hash. Detection needs an external anchor: forward entries (or the latest hash) to WORM storage. **Not implemented** (documented in `docs/compliance.md`). |
| **T**ampering: model changes payment destination | Injected instruction adds `customer_phone` | Destination pinned to the caller before any other check | `eval:payment-link-redirect` | None at the agent layer; see direct Lambda calls above. |
| **R**epudiation | "The agent never sent me that link" / "I never asked for that" / "I never agreed to be recorded" | Every gate, velocity, and tool decision is audited with reason code, salted caller ref, and idempotency key; post-call summary with tool outcomes as facts; `call_disclosure` audit entry per call (disclosure, consent result, recording outcome) | `tests/test_policy_gate.py::test_policy_decision_is_audited_without_transcript_text`, `tests/test_summary.py::test_deliver_audits_and_posts_once_per_call`, `eval:consent-declined-no-recording` | Timestamps come from the host clock and entries aren't signed. The consent keypress itself is only in Twilio's logs. |
| **I**nformation disclosure: recording without consent | A call is recorded in an all-party state without the caller agreeing | Recording off by default; consent prompt by jurisdiction or always; silence or any key but 1 means no; the voice process re-derives the rule before starting a recording | `eval:consent-declined-no-recording`, `eval:consent-no-input-no-recording`, mutations `test_evals_catch_recording_without_consent`, `test_evals_catch_silence_treated_as_consent` | Jurisdiction is number-based (`FromState`), not location. The all-party list is a conservative default, not legal advice. |
| **I**nformation disclosure: identifiers to backends | Caller reads a card number into a ticket | `scrub_args()` removes PAN (Luhn), SSN, bank, DOB, license numbers from every string argument before execution | `eval:card-number-in-ticket`, `tests/test_evals.py::test_evals_catch_pii_reaching_outside_services` | Pattern-based. Misses spoken-word digits ("four one one one…") and non-US formats. Not a DLP. |
| **I**nformation disclosure: card data in keypad mode | Card digits recorded, transcribed, or seen by the model during keypad entry | `PAYMENT_MODE=keypad` hands the call to Twilio `<Pay>` (the agent's stream ends); recording paused first, capture refused if pausing fails; `CaptureGuard` drops keypad tones always and caller audio/transcripts during capture; only `Result` read from the callback | `eval:keypad-payment`, `eval:keypad-recording-pause-fails`, `tests/test_keypad.py`, mutation `test_evals_catch_card_capture_without_pausing_the_recording` | Twilio `<Pay>` and the connector are not exercised here. Parked call state is per process. See `docs/pci.md`. |
| **I**nformation disclosure: logs | PII in the audit log | Arguments fully redacted (`redact()`), caller stored as salted SHA-256, transcripts never logged, policy reasons contain codes not text | `tests/test_tools.py::test_handler_redacts_audits_and_returns_result`, `eval:booking-local-time` (`audit_lacks`) | Phone numbers have a small keyspace: with the salt, the hash can be brute-forced. Keep `AUDIT_SALT` secret and long; the default `change-me` must be replaced. |
| **I**nformation disclosure: internals to the model | Stack traces or hostnames read aloud | Upstream exceptions become a generic `error`; details go to the audit log only | `eval:stripe-outage-retry` (`llm_lacks`), `tests/test_tools.py::test_executor_failure_returns_safe_message` | Handler 4xx messages are passed through on purpose (they're written for the caller). |
| **I**nformation disclosure: traces | Transcripts exported to a tracing backend | Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set; safeguard span events carry only codes and counts | `tests/test_observability.py::test_safeguard_event_lands_on_the_current_span` | Pipecat's own spans include transcript, LLM output, and system prompt attributes. Filter in a collector or use a trusted backend (`docs/observability.md`). |
| **D**enial of service / cost | Caller loops the agent into hundreds of actions, or holds the line open | Velocity per caller and tool; per-call action and payment caps; maximum call duration and idle timeout with a graceful goodbye (`src/agent/call_limits.py`) | `eval:payment-burst`, `eval:booking-burst`, `eval:action-cap-per-call`, `tests/test_call_limits.py` | No per-call model-spend (token) budget (**not implemented**). The goodbye is spoken by the model; if it doesn't, the pipeline still ends after the grace period. Velocity state is per process unless `VELOCITY_BACKEND=redis` (shared; fails closed when Redis is down). No Lambda reserved concurrency set. |
| **E**levation of privilege: tools not granted | Model calls `issue_refund` or an invented function | Default-deny allow-list; catch-all handler routes unknown names to the gate; env can only narrow the list | `eval:unlisted-tool-default-deny`, `tests/test_policy_gate.py::test_fallback_handler_denies_invented_tools`, `tests/test_bot_wiring.py::test_pipeline_registers_a_catch_all_that_denies` | Tool Lambdas are reachable without the agent (see spoofing). |

## OWASP Top 10 for Agentic Applications (ASI01–ASI10)

| ID | Risk | Control in code | Evidence | Residual risk |
|---|---|---|---|---|
| ASI01 | Agent goal hijack | Caller speech can steer the model, so consequential actions are checked outside it: destination pinning, policy gate (risk score, caps), velocity | `eval:payment-link-redirect`, `eval:social-engineering-handoff`, `tests/test_evals.py::test_evals_catch_a_missing_policy_gate` | What the agent *says* is not filtered; it could still be talked into a wrong verbal statement. Regex scoring misses paraphrases and other languages. |
| ASI02 | Tool misuse and exploitation | Risk tiers, default-deny allow-list, per-call caps, velocity, handler validation (amount limits, ISO timestamps), idempotency | `eval:unlisted-tool-default-deny`, `eval:per-call-payment-cap`, `eval:over-limit-then-corrected`, `eval:replayed-request` | Free-text fields (ticket body, notes) are written to backends after scrubbing; their content is otherwise unchecked. |
| ASI03 | Identity and privilege abuse | Each Lambda gets only its own secrets (SAM template); the agent never sees backend credentials; caller is pseudonymized; high-tier tools need step-up verification, never caller ID | `infra/template.yaml`; `tests/test_policy_gate.py::test_unknown_tool_is_denied_and_name_is_scrubbed`; `eval:caller-id-is-not-identity` | Tool API uses a shared signing secret, not per-workload identity (IAM/SigV4 on the roadmap). Step-up uses a restricted authenticator (`docs/auth.md`). |
| ASI04 | Agentic supply chain | Dependencies declared with version ranges; CI runs lint, the policy validator, tests, and evals on every push; the policy itself is a reviewed file that fails closed if invalid | `.github/workflows/ci.yml`, `tests/test_policy_config.py` | No lockfile, SBOM, or hash pinning. The browser console loads Pyodide from a CDN without Subresource Integrity. **Not implemented.** |
| ASI05 | Unexpected code execution | The agent has no code-execution or shell tools; tool arguments are treated as data, never evaluated | `tests/test_tools.py::test_specs_cover_every_executor` (fixed tool set) | Low. Adding a code or browser tool would need its own controls. |
| ASI06 | Memory and context poisoning | No memory across calls; each call has a fresh context and `CallPolicy`; summaries take tool outcomes as facts, not the model's claims | `tests/test_summary.py::test_anthropic_summary_is_forced_through_a_tool_and_sees_only_redacted_text` | Within a call, the caller can poison the context. CRM notes written from summaries can carry caller-supplied text to staff (identifiers scrubbed, wording not). |
| ASI07 | Insecure inter-agent communication | Not applicable: single agent, no agent-to-agent protocol | — | Revisit if a second agent is added. |
| ASI08 | Cascading failures | Upstream errors become a safe `error`; failed calls release velocity slots and idempotency claims; a created-but-unsent payment link returns `sms_failed` instead of retrying into a duplicate | `eval:stripe-outage-retry`, `eval:sms-outage`, `eval:crm-outage-lead` | No circuit breaker or backoff. The idempotency store fails open if DynamoDB is down (documented in `docs/architecture.md`). |
| ASI09 | Human-agent trust exploitation | Fixed AI disclosure played by Twilio before the agent connects; system prompt forbids claiming to be a person; handoff guidance tells the model not to promise times; the agent can only report outcomes tools returned; payment URLs aren't returned to be read aloud | `eval:ai-disclosure-always`, `tests/test_call_start.py`, `tests/test_policy_gate.py::test_guidance_never_reveals_detection_details`, `eval:payment-burst` (`absent_keys: [url]`) | The model could still be talked into saying it is human mid-call (prompt rule only; output isn't filtered). |
| ASI10 | Rogue agents | Bounded autonomy: per-call action ceiling, velocity limits, default deny; every action audited | `eval:action-cap-per-call`, `tests/test_evals.py::test_evals_catch_missing_velocity_limits` | No runtime kill switch beyond configuration (`POLICY_ALLOWED_TOOLS`) and a restart. |

## OWASP Top 10 for LLM Applications 2025

| ID | Risk | Control in code | Evidence | Residual risk |
|---|---|---|---|---|
| LLM01 | Prompt injection | Same as ASI01: consequential actions are gated by code, not by the prompt | `eval:payment-link-redirect`, `eval:social-engineering-handoff` | Spoken injection into free-text tool fields passes through (scrubbed of identifiers only). |
| LLM02 | Sensitive information disclosure | Identifier scrubbing before tools; redacted audit; redacted transcript before the summary model; payment links keep card data off the call | `eval:card-number-in-ticket`, `tests/test_summary.py::test_anthropic_summary_is_forced_through_a_tool_and_sees_only_redacted_text` | The realtime model provider receives raw audio and transcripts. Use provider data-retention controls (see `docs/compliance.md` checklist). |
| LLM03 | Supply chain | See ASI04 | — | See ASI04. |
| LLM04 | Data and model poisoning | Not applicable: no fine-tuning or training on call data in this repository | — | Applies to the model providers. |
| LLM05 | Improper output handling | Tool calls are validated by the gate and handlers; the model never reaches backends directly; handler inputs are parsed, not evaluated | `eval:unparseable-time-then-fixed`, `tests/test_policy_gate.py::test_payment_handler_rejects_non_finite_amounts` | Free text written to Zendesk/CRM is rendered by those systems; their own escaping applies. |
| LLM06 | Excessive agency | A fixed tool set with declared tiers; default deny; per-call caps; payment destination not chosen by the model | `eval:unlisted-tool-default-deny`, `eval:per-call-payment-cap`, `eval:action-cap-per-call` | Medium-tier tools are not handed off on risk by default (`POLICY_HANDOFF_MIN_TIER=high`). |
| LLM07 | System prompt leakage | The prompt holds no secrets or thresholds; policy limits live in code and env, and refusal guidance doesn't reveal them | `tests/test_policy_gate.py::test_guidance_never_reveals_detection_details` | The prompt itself can be extracted; nothing in it is sensitive. |
| LLM08 | Vector and embedding weaknesses | Not applicable: no retrieval or vector store | — | — |
| LLM09 | Misinformation | Booking/payment confirmations come from tool results; the summary is told to use tool outcomes as the record | `tests/test_summary.py::test_rule_based_outcomes` | The agent can still answer general questions wrongly. No grounding or output checks. |
| LLM10 | Unbounded consumption | Velocity, per-call caps, maximum call duration, idle timeout | `eval:payment-burst`, `eval:action-cap-per-call`, `tests/test_call_limits.py` | No token budget (**not implemented**). |

## Highest-priority gaps

1. Anchor the audit chain externally (WORM storage or periodic hash publication).
2. Plug a real SIM-swap / number-port signal into the `RiskSignalProvider` hook, and collect one-time codes by keypad so they stay away from the model.
3. Workload identity for the tool API (IAM/SigV4) instead of a shared signing secret.

Done in 0.5.0–0.6.0 (formerly on this list): tool API authentication (HMAC-signed requests), step-up verification before high-tier tools, keypad card capture, AI disclosure and recording consent at call start, a shared (Redis) velocity store, a maximum call duration and idle timeout.
