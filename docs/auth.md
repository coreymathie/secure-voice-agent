# Step-up verification

Caller ID tells you which number the phone network says is calling. It is easy to spoof, so it is not identity. Before a high-tier tool runs (`take_payment`, `update_contact`), the caller has to prove they hold the phone number on the customer's record by reading back a one-time code sent to that number.

This page covers what is implemented, how it maps to NIST SP 800-63B, the risk we accept by using a code over SMS or a voice call, and what a deployment has to supply.

Code: [`src/safeguards/step_up.py`](../src/safeguards/step_up.py), the step-up rule in [`src/safeguards/policy_gate.py`](../src/safeguards/policy_gate.py), wiring in [`src/agent/tools.py`](../src/agent/tools.py) and [`src/agent/bot.py`](../src/agent/bot.py). Tests: [`tests/test_step_up.py`](../tests/test_step_up.py). Evals: `caller-id-is-not-identity`, `step-up-then-pay`, `otp-lockout`, `sim-swap-signal`, `account-takeover-sequence`.

## Flow

```
 caller asks to pay
   │
   ▼
 take_payment ──▶ policy gate: tier high, call not verified ──▶ "step_up_required"
                                                               (guidance: offer a code)
   │
   ▼
 send_verification_code ──▶ CrmLookup.find_by_caller_id(caller ID)   ← caller ID used only here
                             │  no record / no phone on file ─────────▶ require_human
                             ▼
                            RiskSignalProvider.signals(record, phone on file)
                             │  recent_sim_swap / recent_number_port ─▶ require_human
                             ▼
                            Verifier.start(phone on file, sms|call)   ← never a number from the caller or model
   │
   ▼
 caller reads the code back
   │
   ▼
 verify_caller ──▶ Verifier.check(phone on file, code)
                    │ match ─▶ CallPolicy.mark_verified("otp_sms", customer_id)
                    │ wrong ─▶ invalid_code; 3rd wrong code ─▶ lock ─▶ require_human
   ▼
 take_payment ──▶ policy gate: verified ──▶ caps, velocity, scrub, audit, execute
```

Order inside the gate: allow-list, then the social-engineering score, then the account-takeover rule, then step-up. A caller who has already tripped the social-engineering threshold is handed to a person rather than offered a code, because a code sent to a hijacked number would pass.

## What the code enforces

| Property | How | Evidence |
|---|---|---|
| Caller ID is a lookup key, never identity | `StepUpSession.record()` looks the caller up once; only `verify_caller` with a correct code marks the call verified | eval `caller-id-is-not-identity`; `tests/test_step_up.py::test_correct_code_verifies_the_call_and_the_gate_lets_payments_through` |
| Codes go only to the phone on file | The destination is `CustomerRecord.phone_on_file`; any number in the tool arguments is ignored | eval `caller-id-is-not-identity` (`otp_to`, `upstream_lacks`); `test_code_goes_to_the_phone_on_file_not_caller_id_or_a_supplied_number`; mutation test `test_evals_catch_codes_sent_to_caller_id` |
| High-tier tools need a verified call | `PolicyConfig.step_up_min_tier` (default `high`); the gate returns `step_up_required` | eval `step-up-then-pay`; mutation test `test_evals_catch_step_up_being_skipped` |
| Attempt limit and lockout | 3 wrong codes per call lock the session (`STEP_UP_MAX_FAILED_ATTEMPTS`); after that, `verify_caller`, `send_verification_code`, and high-tier tools hand off | eval `otp-lockout`; `test_three_wrong_codes_lock_and_the_gate_hands_off`; mutation test `test_evals_catch_a_missing_lockout` |
| Across calls | Velocity rules per caller number: 3 codes per 10 minutes, 6 checks per hour (`src/safeguards/velocity.py`) | `tests/test_step_up.py::test_rejected_step_up_request_does_not_use_up_velocity` |
| Send limit (cost, SMS pumping) | 3 codes per call (`STEP_UP_MAX_SENDS_PER_CALL`) plus the velocity rule above | `test_send_limit_per_call` |
| Single use, bounded lifetime | Simulated verifier: a new code replaces the old one, a used code is gone, codes expire after `STEP_UP_CODE_TTL_SECONDS` (default 600). Twilio Verify enforces its own expiry and single use | `test_simulated_verifier_codes_are_single_use_expire_and_replace`, `test_check_before_send_and_after_expiry` |
| Codes are random and compared in constant time | `secrets.randbelow`, `hmac.compare_digest` (simulated verifier) | `src/safeguards/step_up.py` |
| The code never reaches a log | `verify_caller`'s `code` is replaced with `[REDACTED_SECRET]` before the `tool_call` audit entry; the phone on file is recorded only as a short hash | eval `caller-id-is-not-identity` (`audit_lacks`); `test_handler_masks_the_code_and_keeps_audit_detail_from_the_model` |
| The model isn't told why a code wasn't sent | Reasons such as a SIM-swap signal go to the audit log (`_audit` detail); the model gets a generic "a team member will follow up" | eval `sim-swap-signal` (`llm_lacks`); `test_sim_swap_signal_blocks_the_pstn_and_the_reason_stays_out_of_the_result` |
| Account-takeover sequence | After `update_contact` succeeds, any tool that moves money on the same call hands off, verified or not | eval `account-takeover-sequence`; `test_contact_change_then_payment_hands_off_even_when_verified`; mutation test `test_evals_catch_a_missing_takeover_rule` |
| Which record changes isn't the model's choice | `update_contact` gets `customer_id` from the verified session; a model-supplied one is dropped | eval `account-takeover-sequence` (`upstream_lacks: [cust_999]`); `test_update_contact_record_comes_from_the_verified_session` |
| Fails closed | No CRM lookup configured (`NoCrm`) or no verifier (`UnavailableVerifier`): nobody can verify, so high-tier tools hand off | `test_no_record_hands_off_without_sending`, `test_unconfigured_verifier_hands_off` |

## NIST SP 800-63B: how this maps

This is an engineering reading of SP 800-63B (Digital Identity Guidelines, Authentication and Lifecycle Management) to explain the design choices. It is **not** a conformance claim, and a phone call to a small business is not the federal digital-identity setting the guideline was written for. Check the current revision's text (SP 800-63B-4) for exact requirements before relying on any of this.

- **Authenticator type.** A code sent by SMS or a voice call is an out-of-band authenticator that uses the public switched telephone network (PSTN). SP 800-63B classifies PSTN delivery as a **restricted** authenticator because of SIM swap, number porting, and SS7-style interception.
- **Pre-registered destination.** The out-of-band secret has to go to a number associated with the subscriber beforehand, not one supplied during the authentication. Here that is `phone_on_file` from the CRM record; the caller's number and the model's arguments are never used as the destination.
- **Risk indicators before using the PSTN.** SP 800-63B says verifiers should consider indicators such as device swap, SIM change, and number porting before sending a secret over the PSTN. That is the `RiskSignalProvider` hook: if it reports `recent_sim_swap`, `recent_number_port`, or `recent_contact_change` for the number on file, no code is sent and the call hands off.
- **Secret properties.** Codes are random, at least 6 digits, single use, and short-lived (10 minutes by default here).
- **Rate limiting.** SP 800-63B requires limiting consecutive failed attempts. This repo is much stricter than the guideline's ceiling: 3 per call, then lockout, plus a per-number velocity limit across calls.
- **Not covered:** identity proofing (who the customer is in the first place), phishing resistance (a caller can be socially engineered into reading their code to an attacker), and authenticator lifecycle (binding, rebinding, and revoking the number on file) are outside this repo.

## Risk acceptance: SMS / voice one-time codes (restricted authenticator)

Use this as a starting template; the deployer owns the decision and should record who accepted it and when.

| Item | Statement |
|---|---|
| What is accepted | One-time codes delivered by SMS or voice call to the phone on file, used as step-up before payment links and contact changes on inbound calls. |
| Why | Callers on a phone line have the phone; there is no app or hardware key to rely on. The actions protected are bounded (per-call caps, velocity limits, pay-by-link to the calling number). |
| Known weaknesses | SIM swap and number porting move the number to an attacker; SS7 and carrier-level interception; malware on the handset; a caller can be tricked into reading their own code to an impostor. |
| Mitigations in this repo | Codes only to the number on file; SIM-swap / porting hook (`RiskSignalProvider`) that blocks the PSTN and hands off; 3-attempt lockout per call; per-number velocity across calls; social-engineering score checked before a code is offered; account-takeover rule (contact change then payment hands off); every step audited without the code or the number. |
| Mitigations the deployer must add | A real `RiskSignalProvider` (carrier or vendor SIM-swap / porting lookups); an alternative that is not restricted for customers who ask for one (for example a callback by staff, or verification in an authenticated app or portal); telling customers that SMS codes carry these risks; a review of handoffs. |
| Residual risk | An attacker who controls the number on file and isn't caught by the risk signal can pass step-up. The per-call caps and the takeover rule bound what they can do on that call. |
| Review trigger | A confirmed takeover, a change in the tools that require step-up, or availability of a stronger authenticator for this customer base. |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `POLICY_STEP_UP_MIN_TIER` | `high` | Lowest tool tier that needs a verified call. `off` disables step-up (not recommended). |
| `STEP_UP_MAX_FAILED_ATTEMPTS` | `3` | Wrong codes per call before lockout. |
| `STEP_UP_MAX_SENDS_PER_CALL` | `3` | Codes one call can send. |
| `STEP_UP_CODE_TTL_SECONDS` | `600` | Lifetime of a code (simulated verifier and the session's own check). |
| `STEP_UP_CHANNELS` | `sms,call` | Channels the agent may choose. |
| `TWILIO_VERIFY_SERVICE_SID` | — | Turns on `TwilioVerifyVerifier`. Without it, nobody can verify and high-tier tools hand off. |
| `CRM_LOOKUP_FILE` | — | Local development only: a JSON list of `{customer_id, phone_on_file, lookup_numbers}`. In production, implement `CrmLookup` against your CRM. |

## What is real and what is simulated

- **Real, tested here:** the session logic, the gate rules, lockout, masking, the takeover rule, the `update_contact` Lambda's validation, and the `TwilioVerifyVerifier` call shape (against a stand-in client).
- **Not exercised here:** Twilio Verify itself, any CRM lookup other than the in-memory one, and any real SIM-swap data source. No `RiskSignalProvider` for a carrier or vendor ships with this repo.

## Known limitations

- **The code passes through speech recognition and the model.** The caller reads the code aloud, so it is in the audio, the transcript, and the model provider's session. It is masked in the audit log, single use, and short-lived. Collecting it by keypad (`<Gather input="dtmf">`) would keep it away from the model; that is **not implemented**.
- **Lockout is per call; the cross-call limit is per caller number.** It uses the velocity store, which is per process unless a shared store is configured.
- **Verification lasts for the call.** There's no re-verification after a long idle period.
- **No notification to the old contact** when `update_contact` changes details (a common takeover mitigation). Not implemented; see `src/handlers/update_contact.py`.
- **Caller ID lookup only.** Customers calling from a number not on their record can't be found; offering lookup by account number would need its own design (and more guessing protection).
