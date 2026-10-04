# Deploy

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

Not using one of the integrations? Put any placeholder value for its keys; that handler simply won't be called.

## 2. Deploy the Lambda side

```bash
cd infra
sam build -t template.yaml
sam deploy --guided --parameter-overrides \
  AgentPublicWsUrl=wss://voice-agent.fly.dev/stream BusinessTimezone=America/New_York
```

Stack outputs:

- `ApiBaseUrl`: set as `LAMBDA_BASE_URL` on the voice process
- `VoiceWebhookUrl`: paste into your Twilio number's "A call comes in" webhook (HTTP POST)

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

If the Fly hostname differs from what you passed to SAM, redeploy SAM with the right `AgentPublicWsUrl`.

Post-call summaries use whichever model key is already set (`OPENAI_API_KEY` for the default provider). To choose explicitly, also set `SUMMARY_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`, or `SUMMARY_PROVIDER=none` for rule-based notes only. Notes land on the caller's GoHighLevel contact when `GOHIGHLEVEL_API_KEY` is in the secret; otherwise they are kept in the audit log only.

## 4. Smoke test

Call the Twilio number. Then:

```bash
flyctl logs                                   # call started / call ended
flyctl ssh console -C "tail -n 20 logs/audit.jsonl"
```

After hanging up you should see a `call_summary` entry at the end of the audit log, and a new note on the caller's CRM contact.

Before going live, run the evals against your configuration (for example after changing velocity rules or `MAX_PAYMENT_USD`):

```bash
python -m evals.run
```

## Rough running cost

- Lambda + HTTP API + DynamoDB on-demand: cents per month at low volume
- Fly.io, one always-on shared-cpu machine: a few dollars per month
- OpenAI Realtime audio and Twilio voice minutes make up most of the per-call cost. Check current pricing pages before quoting a number to anyone.
