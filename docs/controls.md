# Controls and governance mapping

This maps the controls implemented in this repo to the functions of the NIST AI Risk Management Framework (AI RMF 1.0) and the risk categories of NIST AI 600-1 (the Generative AI Profile), with notes on PCI DSS scope and the EU AI Act. It is an engineering mapping to help a reviewer find the evidence, **not** a compliance attestation or legal advice.

Status values: **implemented** (code + test in this repo), **partial** (code exists, with stated limits), **not implemented** (roadmap).

## Control inventory

| # | Control | Where | Evidence | Status |
|---|---|---|---|---|
| C1 | Default-deny tool allow-list with risk tiers | `src/safeguards/policy_gate.py` (`TOOL_POLICIES`, `CallPolicy.decide`), catch-all in `src/agent/bot.py` | `eval:unlisted-tool-default-deny`, `tests/test_policy_gate.py` | implemented |
| C2 | Social-engineering risk scoring with handoff | `policy_gate.py` (`SIGNALS`, `score_turns`); transcript source in `bot.py` | `eval:social-engineering-handoff`, `eval:urgent-but-benign`, `tests/test_evals.py::test_evals_catch_missing_risk_scoring` | partial: regex signals, English only; S2S transcripts can lag (ADR 0001) |
| C3 | Per-call blast-radius caps (links, USD, actions) | `policy_gate.py` (`PolicyConfig`, `record_outcome`) | `eval:per-call-payment-cap`, `eval:action-cap-per-call` | implemented |
| C4 | Per-caller velocity limits | `src/safeguards/velocity.py`; shared store `src/safeguards/velocity_redis.py` (`VELOCITY_BACKEND=redis`) | `eval:payment-burst`, `eval:payment-ladder`, `eval:booking-burst`; `tests/test_velocity_redis.py` (same decisions as the in-memory store, limits across instances, concurrent checks, fail closed); `tests/test_evals.py::test_every_scenario_passes_with_the_redis_velocity_store` | implemented (in memory by default; Redis store tested against an in-memory stand-in and fakeredis, not a live Redis server) |
| C5 | Payment destination pinned to the caller | `make_handler()` in `src/agent/tools.py` | `eval:payment-link-redirect` | implemented (agent path only; see C13) |
| C6 | Identifier scrubbing before tools | `scrub_args()` in `src/safeguards/pii_redactor.py` | `eval:card-number-in-ticket` | partial: pattern-based |
| C7 | Redacted, hash-chained audit log of every decision | `src/safeguards/audit_log.py`, `make_handler()` | `tests/test_safeguards.py::test_audit_chain_detects_tampering`, every eval checks the chain | partial: no external anchor / WORM |
| C8 | Caller pseudonymization | `caller_ref()` in `tools.py` | `tests/test_tools.py::test_handler_redacts_audits_and_returns_result` | implemented |
| C9 | Idempotent tool execution | `src/handlers/_common.py` | `eval:replayed-request`, `tests/test_handlers.py` | implemented (fails open if DynamoDB is down) |
| C10 | Safe failure handling | `make_handler()`, `take_payment.py` (`sms_failed`) | `eval:stripe-outage-retry`, `eval:sms-outage`, `eval:crm-outage-lead` | implemented |
| C11 | Pay-by-link (no card data by voice) | `src/handlers/take_payment.py`, ADR 0004 | `eval:over-limit-then-corrected`, `tests/test_handlers.py::test_payment_rejects_over_limit` | implemented |
| C12 | Webhook signature verification | `twilio_voice_hook.py`, `server.py` | `tests/test_handlers.py::test_voice_hook_rejects_bad_signature` | implemented when `TWILIO_AUTH_TOKEN` is set |
| C13 | Tool API authentication | HMAC-SHA256 request signing in `src/handlers/_common.py` (`verify_signature`, `idempotent`); signing in `tools.tool_request()` | `tests/test_request_signing.py`; every eval sends signed requests | implemented (shared secret; IAM/SigV4 not implemented) |
| C14 | Evals with mutation tests in CI, plus simulated multi-turn callers | `evals/` (`run.py`, `simulate.py`, `personas.yaml`), `tests/test_evals.py`, `tests/test_simulate.py`, `.github/workflows/ci.yml` | CI job summary | implemented (text-level; no live model or audio) |
| C15 | Tracing with safeguard span events | `src/agent/tracing.py`, `src/safeguards/telemetry.py` | `tests/test_observability.py` | implemented (off by default) |
| C16 | Post-call summary from redacted transcript, tool outcomes as facts | `src/agent/summary.py` | `tests/test_summary.py` | implemented |
| C17 | AI disclosure and recording consent at call start | `src/handlers/call_start.py` (TwiML), `twilio_voice_hook.py`, `twilio_consent.py`, `src/agent/server.py`, `src/agent/disclosure.py` (recording decision + `call_disclosure` audit) | `eval:ai-disclosure-always`, `eval:consent-declined-no-recording`, `eval:consent-no-input-no-recording`, `eval:consent-granted-recording`, `eval:one-party-state-notice`, `tests/test_call_start.py` | implemented (jurisdiction from the caller's number; all-party list configurable, not legal advice) |
| C18 | Step-up authentication for high-tier tools | `src/safeguards/step_up.py` (`StepUpSession`, `CrmLookup`, `Verifier`, `RiskSignalProvider`); gate rule in `policy_gate.py` (`step_up_required`, `step_up_locked`); `docs/auth.md` | `eval:caller-id-is-not-identity`, `eval:step-up-then-pay`, `eval:otp-lockout`, `eval:sim-swap-signal`, `tests/test_step_up.py` | implemented (SMS/voice OTP, a restricted authenticator; Twilio Verify and SIM-swap data sources not exercised here) |
| C19 | Human review queue for handoffs | — (handoffs are audited and the model offers a callback; nothing routes them to a person yet) | `audit event: policy_handoff`, `handoff_required` | **not implemented** |
| C20 | Account-takeover sequence rule (contact change, then payment) | `policy_gate.py` (`contact_change_then_payment`), `src/handlers/update_contact.py` | `eval:account-takeover-sequence` | implemented (per call) |
| C21 | PCI keypad capture via Twilio `<Pay>` with recording pause (fail closed) and capture guard | `src/handlers/keypad_payment.py`, `twilio_pay_result.py`, `pay_twiml.py`, `src/agent/capture.py`, `src/agent/resume.py`; `docs/pci.md` | `eval:keypad-payment`, `eval:keypad-recording-pause-fails`, `eval:keypad-over-limit`, `tests/test_keypad.py` | implemented (opt-in `PAYMENT_MODE=keypad`; Twilio side simulated) |
| C22 | Policy as code with schema validation, fail-closed loading, and eval gating | `config/policy.yaml`, `src/safeguards/policy_config.py`, `evals/run.py --policy`; `docs/policy.md` | `tests/test_policy_config.py` (parity with code defaults, invalid files, startup refusal), mutation tests with edited policy files in `tests/test_evals.py` | implemented |
| C23 | Maximum call duration and idle timeout with a graceful goodbye | `src/agent/call_limits.py` (`CallTimer`, `watch_call`, activity observer), `run_bot` in `src/agent/bot.py` | `tests/test_call_limits.py` (timer, goodbye-then-end sequence, `call_ended_by_limit` audit through `run_bot`) | implemented (goodbye wording comes from the model) |

## NIST AI RMF 1.0

| Function | What it asks for (summary) | Controls here | Gaps |
|---|---|---|---|
| **GOVERN** | Policies, accountability, documented risk tolerance | C22 policy as code: tool tiers, caps, risk signals, step-up, and velocity in the reviewed `config/policy.yaml`, schema-validated, fail-closed, tested against every eval; ADRs 0001–0004 record decisions; `docs/auth.md` records the SMS OTP risk acceptance; this file and the threat model | No named owner/approval records (that's the version-control platform's review settings, out of scope for a starter) |
| **MAP** | Context, intended use, impacts, threats | `docs/threat-model.md` (STRIDE, OWASP Agentic, OWASP LLM 2025); tool tiers state why each tool is risky (`ToolPolicy.why`) | No formal impact assessment for a specific deployment |
| **MEASURE** | Test, evaluate, monitor | C14 evals and mutation tests; simulated callers with task success, correct refusals, false-positive rate, and handoffs over scripted personas (`python -m evals.simulate`, `docs/simulation.md`); C15 tracing; `evals/latency.py` for p50/p95 time-to-first-audio from your traces | Evals and the simulator are text-level with no live model or speech; the false-positive rate is over 14 hand-written personas, not real calls; no latency numbers published (none measured here) |
| **MANAGE** | Prioritize and act on risks, respond, recover | C2/C3/C4 escalate to a person instead of acting; C10 safe failure; C7 audit trail for incident review | C19 handoff queue; no kill switch beyond config + restart |

## NIST AI 600-1 (Generative AI Profile) risk categories

| GAI risk | Relevance here | Controls | Gaps |
|---|---|---|---|
| Information security | Prompt injection and social engineering steering tool use | C1, C2, C3, C5, C12, C13, C18, C20 | Restricted authenticator (SMS OTP); no SIM-swap data source shipped |
| Data privacy | Callers speak identifiers; transcripts go to providers | C6, C7, C8, C11, C16 | Raw audio reaches the model provider; retention is a provider setting |
| Confabulation | Agent claiming an action happened when it didn't | Tool results are the source of truth; summaries use outcomes as facts (C16); payment URL not returned | Verbal answers aren't checked |
| Human-AI configuration | When a person must take over; over-reliance | Handoff statuses with model guidance (C2, C3, C4); AI disclosure on every call (C17) | C19 review queue |
| Information integrity | Records written to CRM/tickets | Audit trail (C7); scrubbing (C6) | Free-text content isn't verified |
| Value chain and component integration | Third-party models, Pipecat, SaaS APIs | Provider abstraction (ADR 0001); CI | No SBOM/lockfile; demo loads Pyodide from a CDN without SRI |

Other AI 600-1 categories (CBRN, violent or obscene content, harmful bias, IP, environmental) are not specifically addressed by this tool-layer code; they depend on the chosen model provider's safeguards and deployment policy.

## PCI DSS scope-reduction notes

- **Design intent:** cardholder data (CHD) should never enter the voice system. Payments use a hosted Stripe Payment Link sent by SMS (C11, ADR 0004), so card entry happens on the processor's page.
- **What the code does when a caller reads a card number anyway:** Luhn-valid PANs are removed from tool arguments before they reach Zendesk, the CRM, or the calendar (C6), and from audit entries and the summary transcript. `call_summary` scrubs again before writing the CRM note.
- **What it can't do:** the spoken digits were already in the call audio, the speech-to-text output, and the model provider's session. Scrubbing reduces where CHD *persists* in this system; it doesn't make the voice path out of scope. Use provider zero-retention options, keep call recording off (or use a provider's PCI pause/redaction feature), and have a QSA assess the actual deployment.
- **Keypad (DTMF) capture** (`PAYMENT_MODE=keypad`, C21): Twilio `<Pay>` collects the card, the recording is paused first (or capture is refused), the agent's media stream is ended for the duration, and keypad tones are always dropped from the pipeline. Boundary diagram and what is simulated: [`pci.md`](pci.md).
- This repo has not been assessed against PCI DSS.

## EU AI Act, Article 50 (transparency for systems that interact with people)

Article 50(1) requires that people be informed they are interacting with an AI system unless that's obvious from context. These transparency obligations apply from 2 August 2026.

**Status: implemented (C17).** Every incoming call hears a fixed disclosure (`AI_DISCLOSURE_TEXT`, default "You're talking with an A.I., not a person.") played by Twilio from the webhook's TwiML before the media stream to the agent connects, so it doesn't depend on the model. The system prompt also tells the agent to confirm it is an AI if asked and never claim to be a person. The voice process audits `call_disclosure` for each call, including `disclosure: missing` if a call reaches the agent without the webhook's TwiML. Evidence: `eval:ai-disclosure-always` (disclosure is the first thing played), `tests/test_call_start.py`. Whether the wording and timing satisfy Article 50 for a given deployment is for the deployer's counsel; this is an engineering control, not a legal opinion. Recording-consent rules vary by US state; see `docs/compliance.md`.
