# Corey Mathie, 2026
# Voice process image (Fly.io / ECS). The Lambda handlers are packaged separately by `sam build`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The reviewed policy file; the process refuses to start if it's invalid (src/safeguards/policy_config.py).
COPY config ./config

RUN useradd --create-home --uid 10001 agent && mkdir -p /app/logs && chown -R agent /app
USER agent

EXPOSE 8080
CMD ["python", "-m", "src.agent.server"]
