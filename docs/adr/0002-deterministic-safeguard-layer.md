# ADR 0002: A deterministic safeguard layer outside the model, not model self-policing

- Status: accepted
- Date: 2026-10
- Code: `src/agent/tools.py` (`make_handler`), `src/safeguards/`

## Context

The agent can move money (payment links), write into business systems (calendar, tickets, CRM), and handle personal data spoken aloud. The model decides *when* to call a tool. The question is who decides whether that call may run.

Options:

1. **Model self-policing:** put the rules in the system prompt ("never send more than one payment link", "don't text a link to another number") and trust the model.
2. **A second model as a judge:** another LLM call classifies each tool call.
3. **Deterministic code between the model and the tools.**

Prompt rules are useful guidance but not a control: a caller can talk a model out of them (prompt injection, social engineering), behavior varies between model versions, and you can't prove after the fact why a decision was made. A judge model has the same problems one level up, plus latency and cost on every tool call.

## Decision

Every tool call goes through `make_handler()`, which applies plain-Python checks in a fixed order before anything executes:

0. Pin the payment destination to the calling number (model-supplied `customer_phone` is dropped).
1. **Policy gate** (`policy_gate.py`): default-deny allow-list with risk tiers; social-engineering score over the caller's finalized turns; per-call caps on payment links, USD, and actions.
2. **Velocity** (`velocity.py`): per-caller sliding windows.
3. **Scrub and audit** (`pii_redactor.py`, `audit_log.py`): remove government and financial identifiers from arguments, then write a redacted entry to the hash-chained log.
4. **Execute** with the tool-call id as idempotency key.

The model still gets guidance: each refusal returns a `status` and a `reason` written for the model to act on ("A team member needs to handle this request. Don't retry it on this call..."). The prompt tells the model how to handle each status. Guidance never reveals thresholds or which phrases were flagged.

The social-engineering scorer is deliberately simple (weighted regular expressions over five signal families). It is transparent, testable, and has no false sense of sophistication. It is one layer; the caps and velocity limits hold even if it misses.

## Consequences

- Positive: same inputs, same decision. Every decision is in the audit log with a reason code, so it can be replayed and reviewed.
- Positive: the controls are tested without a model. The evals replay tool-call sequences through the real stack, and mutation tests in `tests/test_evals.py` switch off one control at a time (policy gate, risk scoring, per-call counters, velocity, scrubbing, idempotency) and confirm the evals fail.
- Positive: no added model latency or cost per tool call.
- Negative: regex scoring has false negatives (paraphrase, other languages) and false positives (a caller who really is the owner). The default threshold needs two signal families or repeated pressure; `evals/scenarios.yaml` includes a benign-urgency scenario to keep false positives in check. Tune with `POLICY_RISK_THRESHOLD` against your own call data.
- Negative: rules live in code and environment variables. Policy-as-code (versioned, reviewed policy files evaluated by an engine such as OPA/Cedar) is on the roadmap.
