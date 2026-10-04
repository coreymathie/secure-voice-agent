# Policy as code

The rules the policy gate, velocity limits, and step-up verification enforce live in one reviewed file: [`config/policy.yaml`](../config/policy.yaml). Changing what the agent may do is a pull request against that file, with a validator and the full call-eval suite as checks.

## What the file holds

| Section | Controls | Used by |
|---|---|---|
| `tools` | The allow-list: each tool's risk tier, whether it changes state, moves money, or changes contact details, and `enabled` | `policy_gate.CallPolicy` (default deny for anything not listed) |
| `caps` | Per-call payment links, cumulative USD, completed actions | `policy_gate.CallPolicy` |
| `risk` | Social-engineering threshold, handoff tier, score ceiling, and each signal's weight and regex patterns | `policy_gate.score_turns` |
| `step_up` | Tier that needs a verified caller (`off` disables), attempt and send limits, code lifetime, channels, risk signals that block the PSTN | `policy_gate`, `step_up.StepUpSession` |
| `velocity` | Per-caller sliding windows and their action (`deny`, `require_human`, `flag`) | `velocity.VelocityStore` |

## Loading and failing closed

`src/safeguards/policy_config.py` parses the file with a strict schema (pydantic models with unknown keys forbidden), then checks what the schema can't: every regex compiles and every velocity rule names a declared tool. Any problem raises `PolicyConfigError` listing each bad field by path, for example:

```
proposed.yaml: invalid policy
  tools.log_lead.tier: Input should be 'low', 'medium' or 'high'
```

The voice process loads the policy when `src/agent/server.py` is imported, so an invalid file stops it from starting; it never falls back to a guess. Which file:

1. `POLICY_FILE`, if set (a missing file is an error);
2. otherwise `config/policy.yaml` (shipped in the Docker image);
3. otherwise, only if that file doesn't exist at all, the code defaults.

`POLICY_*` and `STEP_UP_*` environment variables still apply on top of the loaded file, as in 0.5.0. They can narrow the allow-list but never add a tool the file doesn't declare.

Every call's audit log starts with a `call_started` entry carrying the first 12 hex characters of the policy file's SHA-256, so any decision can be traced to the exact policy that made it.

## Review workflow

```bash
python -m src.safeguards.policy_config config/policy.yaml    # validate; prints a summary and the sha256
python -m evals.run --policy proposed.yaml                    # run all call scenarios against a proposed policy
pytest tests/test_policy_config.py                            # parity with the code defaults, fail-closed cases
```

CI runs the validator, the tests, and the evals on every push. The evals always use the policy file (never the machine's environment), so a change that weakens a control shows up as a failing scenario. Two mutation tests do exactly that with edited copies of the file: raising the lockout to 99 attempts fails `otp-lockout`, and lowering the risk threshold to 1 fails `urgent-but-benign` (`tests/test_evals.py`).

## Parity with the code defaults

The code keeps its own defaults (`TOOL_POLICIES`, `SIGNALS`, `PolicyConfig()`, `DEFAULT_RULES`, `StepUpConfig()`), because the browser demo runs `policy_gate.py` under Pyodide without the YAML loader. `tests/test_policy_config.py` proves the shipped file and those defaults are identical field by field, pattern by pattern, and that both make the same decision for every tool across a set of caller turns. Change one without the other and the test fails.

## What it isn't

There is no separate policy engine (OPA, Cedar): the file configures the existing deterministic checks rather than expressing arbitrary rules. Approval records (who reviewed which change) come from your version-control platform's review settings, not from this repo.
