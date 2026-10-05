# Setup

From zero to a working voice agent on a real phone number, running locally.

## 1. Prerequisites

- Python 3.11+
- A Twilio account with one voice-capable phone number
- An OpenAI API key (default provider). For the other providers you'll need Anthropic + Deepgram + ElevenLabs keys, or a Google API key.
- `ngrok` (or any tunnel) so Twilio can reach your machine

The tool handlers (calendar, payments, tickets, leads) are optional for a first call. Without `LAMBDA_BASE_URL` set, the agent will still talk; tool calls will return a safe "didn't go through" message, which the audit log records.

Payments and contact changes need step-up verification ([`auth.md`](auth.md)). To try it locally, point `CRM_LOOKUP_FILE` at a JSON file like `[{"customer_id": "c1", "phone_on_file": "+15555550142", "lookup_numbers": ["+15555550100"]}]` and set `TWILIO_VERIFY_SERVICE_SID`. Without them, those tools hand off to a person.

## 2. Install

```bash
git clone https://github.com/coreymathie/secure-voice-agent.git
cd secure-voice-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 3. Minimum `.env`

```bash
AGENT_PROVIDER=openai_realtime
OPENAI_API_KEY=sk-...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...            # also used to verify that webhooks really come from Twilio
TWILIO_PHONE_NUMBER=+1...
AGENT_PUBLIC_WS_URL=wss://<your-ngrok-host>/stream
AUDIT_SALT=<any long random string>
```

## 4. Run

```bash
python -m src.agent.server     # from the repo root; serves POST /voice and WS /stream on :8765
ngrok http 8765                # second terminal
```

Copy the ngrok host into `AGENT_PUBLIC_WS_URL` and restart the server.

## 5. Point Twilio at it

Twilio console → Phone Numbers → your number → **Voice configuration** → "A call comes in" → Webhook → `https://<your-ngrok-host>/voice` → HTTP POST → Save.

Call the number. The agent answers and greets you.

## 6. Check the audit log

```bash
tail -f logs/audit.jsonl
python -c "from src.safeguards.audit_log import AuditLog; print(AuditLog('logs/audit.jsonl').verify_chain())"
```

`(True, None)` means the chain is intact.

## 7. Turn on the tool handlers

Deploy the Lambda side (see `deploy.md`) and set `LAMBDA_BASE_URL` to the `ApiBaseUrl` output.
