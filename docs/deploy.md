# Deploy

This document is the production deployment runbook. The topology follows ADR 0003: short, stateless handlers on AWS Lambda with per-function secrets, and the long-lived voice process on Fly.io (ECS is the equivalent on AWS). Secrets live in AWS Secrets Manager, and every tool request is signed.

Two targets:

- **AWS**: the voice webhook and four tool handlers on Lambda behind an HTTP API, with a DynamoDB table for idempotency
- **Fly.io**: the long-lived Pipecat voice process

## 1. Store secrets in AWS Secrets Manager

All Lambda secrets come from one JSON secret, resolved at deploy time by the SAM template.

```bash
aws secretsmanager create-secret --name voice-agent/prod --secret-string '{
  "TWILIO_ACCOUNT_SID": "AC...",
  "TWILIO_AUTH_TOKEN": "...",
  "TWILIO_PHONE_NUMBER": "+1...",
  "STRIPE_SECRET_KEY": "sk_live_...",
  "GOOGLE_CALENDAR_ID": "primary",
  "GOOGLE_SERVICE_ACCOUNT_JSON": "<paste the full service-account JSON as a string>",
  "ZENDESK_SUBDOMAIN": "...",
  "ZENDESK_EMAIL": "...",
  "ZENDESK_API_TOKEN": "...",
  "GOHIGHLEVEL_API_KEY": "...",
  "GOHIGHLEVEL_LOCATION_ID": "..."
}'
```

For an integration that is not in use, put any placeholder value for its keys; that handler won't be called.

## 2. Deploy the Lambda side

```bash
cd infra
sam build -t template.yaml
sam deploy --guided --parameter-overrides \
  AgentPublicWsUrl=wss://voice-agent.fly.dev/stream BusinessTimezone=America/New_York
```

Stack outputs:

- `ApiBaseUrl`: set as `LAMBDA_BASE_URL` on the voice process
- `VoiceWebhookUrl`: paste into the Twilio number's "A call comes in" webhook (HTTP POST)

## 3. Deploy the voice process to Fly.io

```bash
flyctl launch --copy-config --no-deploy        # first time only; uses ./fly.toml
flyctl volumes create audit --size 1 --region mia   # persistent audit log
flyctl secrets set \
  AGENT_PROVIDER=openai_realtime \
  OPENAI_API_KEY=... \
  TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... \
  LAMBDA_BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com/prod \
  AGENT_PUBLIC_WS_URL=wss://voice-agent.fly.dev/stream \
  AUDIT_SALT=<random string> \
  BUSINESS_TIMEZONE=America/New_York
flyctl deploy
```

If the Fly hostname differs from the value passed to SAM, redeploy SAM with the right `AgentPublicWsUrl`.

Optional on the voice process: the policy gate's limits (`POLICY_*`, defaults in `.env.example`) and tracing (`OTEL_EXPORTER_OTLP_ENDPOINT`, see [`observability.md`](observability.md)). Set them with `flyctl secrets set` like the rest.

**Signing secret:** add `TOOL_API_SECRET` (a long random string, e.g. `openssl rand -hex 32`) to the Secrets Manager secret and set the same value on the voice process. Tool Lambdas reject every request that isn't signed with it (`401`), including all requests when the secret is missing. See [`adr/0003`](adr/0003-lambda-tools-long-lived-call-worker.md).

**Step-up verification:** payments and contact changes need a caller verified by a one-time code (see [`auth.md`](auth.md)). Create a Twilio Verify service and set `TWILIO_VERIFY_SERVICE_SID` on the voice process, and implement `CrmLookup` in `src/safeguards/step_up.py` against the institution's CRM (`CRM_LOOKUP_FILE` is for local development). Without both, high-tier tools hand off to a person. `POLICY_STEP_UP_MIN_TIER=off` turns step-up off.

Post-call summaries use whichever model key is already set (`OPENAI_API_KEY` for the default provider). To choose explicitly, also set `SUMMARY_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`, or `SUMMARY_PROVIDER=none` for rule-based notes only. Notes land on the caller's GoHighLevel contact when `GOHIGHLEVEL_API_KEY` is in the secret; otherwise they are kept in the audit log only.

## 4. Smoke test

Call the Twilio number. Then:

```bash
flyctl logs                                   # call started / call ended
flyctl ssh console -C "tail -n 20 logs/audit.jsonl"
```

After hang-up, the audit log ends with a `call_summary` entry, and the caller's CRM contact has a new note.

Before going live, run the evals against the deployment's configuration (for example after changing velocity rules or `MAX_PAYMENT_USD`):

```bash
python -m evals.run
```

## Rough running cost

- Lambda + HTTP API + DynamoDB on-demand: cents per month at low volume
- Fly.io, one always-on shared-cpu machine: a few dollars per month
- OpenAI Realtime audio and Twilio voice minutes make up most of the per-call cost. Check current pricing pages before quoting a number to anyone.
