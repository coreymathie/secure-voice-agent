# Corey Mathie, 2026
"""
Local console server (live mode): serves the console in demo/ at /console and
backs it with JSON endpoints that run the repo's real code.

    python -m src.console_server             # http://127.0.0.1:8090/console/
    docker compose up                        # same, in a container

What "live" means here, and what it doesn't:

  - Every tool call goes through the real make_handler (policy gate, step-up,
    velocity, PII scrubbing, hash-chained audit), then to the real Lambda handler
    code in src/handlers/ as an HMAC-signed request. The handlers verify the
    signature (src/handlers/_common.py) and run their own validation and
    DynamoDB-style idempotency, exactly as in the evals.
  - The signing secret (TOOL_API_SECRET) is generated at startup unless you set
    one. It is never returned by any endpoint and never logged.
  - Outside services (Stripe, Twilio, Google Calendar, Zendesk, the CRM,
    DynamoDB) are the eval fakes from evals/harness.py: no keys needed, nothing
    leaves the process.
  - There is no phone call and no language model. Caller turns are typed, and a
    scripted agent (keyword rules) proposes the tool calls. Real calls need
    Twilio and the deployment in docs/deploy.md (src/agent/server.py + Lambda).

Auth: set CONSOLE_TOKEN to require `Authorization: Bearer <token>` on /api/*
(enter it in the console's Settings). The server binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from demo.engine import DEMO_STREAM_URL, VERSION, Console, dispatch
from evals import harness, simulate
from src.agent import tools
from src.handlers import _common, twilio_consent, twilio_voice_hook

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
POLICY_FILE = ROOT / "config" / "policy.yaml"
PERSONAS_FILE = ROOT / "evals" / "personas.yaml"
EVAL_DATA = DEMO_DIR / "data" / "evals.json"

SERVICE_OF = {  # tool -> the fake outside service it depends on (Upstreams.down names)
    "take_payment": "stripe",
    "book_meeting": "calendar",
    "create_ticket": "zendesk",
    "log_lead": "crm",
    "update_contact": "crm",
}
UPSTREAM_FIELDS = (
    "sms",
    "payment_links",
    "calendar_events",
    "tickets",
    "crm_contacts",
    "contact_updates",
    "refunds",
    "keypad_sessions",
    "recordings",
)


class Signing:
    """The console's TOOL_API_SECRET and how many signatures the handlers accepted or refused."""

    def __init__(self, secret: str | None = None):
        configured = secret if secret is not None else os.environ.get("TOOL_API_SECRET", "")
        self.source = "TOOL_API_SECRET from the environment" if configured else "generated at startup"
        self._secret = configured or secrets.token_hex(32)
        self.verified = 0
        self.rejected = 0

    @property
    def env(self) -> dict[str, str]:
        return {"TOOL_API_SECRET": self._secret}

    def status(self) -> dict:
        return {
            "enabled": True,
            "algorithm": "HMAC-SHA256 over timestamp.idempotency_key.body (v1)",
            "headers": [_common.SIGNATURE_HEADER, _common.TIMESTAMP_HEADER, "Idempotency-Key"],
            "max_skew_seconds": _common.MAX_SKEW_SECONDS,
            "secret": "configured (never displayed)",
            "secret_source": self.source,
            "verified": self.verified,
            "rejected": self.rejected,
            "verified_by": "src/handlers/_common.py verify_signature() inside each real Lambda handler",
        }


class LiveWorld:
    """Process-wide state shared by every live call: one lock, the signing secret, and the service patches."""

    def __init__(self, signing: Signing | None = None):
        self.lock = threading.RLock()
        self.signing = signing or Signing()

    @contextmanager
    def patched(self, up: harness.Upstreams, table: harness.MemoryTable):
        """Fake outside services for this call, the console's signing secret, and a signature counter."""
        real_verify = _common.verify_signature
        signing = self.signing

        def counting_verify(event, now=None):
            problem = real_verify(event, now)
            if problem:
                signing.rejected += 1
            else:
                signing.verified += 1
            return problem

        with ExitStack() as stack:
            harness.patch_services(stack, up, table, env=self.signing.env)
            stack.enter_context(patch.object(_common, "verify_signature", counting_verify))
            yield


class LambdaBackends:
    """
    The console engine's backends in live mode: each tool call becomes a signed
    request to the real Lambda handler module, with the eval fakes behind it.
    Same interface as demo/engine.py's SimulatedBackends.
    """

    kind = "lambda"
    label = "Real Lambda handler code (src/handlers/) behind HMAC-signed requests; outside services faked"

    def __init__(self, world: LiveWorld):
        self.world = world
        self.up = harness.Upstreams()
        self.table = harness.MemoryTable()
        self.received: list[dict] = []
        self.requests: list[dict] = []
        self.down: set[str] = set()
        self.payment_mode = "link"
        self.pay_twiml: str | None = None

    def __getattr__(self, name: str) -> Any:
        if name in UPSTREAM_FIELDS:
            return getattr(self.up, name)
        raise AttributeError(name)

    def everything_received(self) -> str:
        return self.up.everything_received()

    def signing_status(self) -> dict:
        return self.world.signing.status()

    def executors(self) -> dict:
        def make(tool: str):
            async def run(args: dict, idem: str) -> dict:
                return self.run(tool, args, idem)

            return run

        return {name: make(name) for name in tools.LAMBDA_TOOLS}

    def run(self, tool: str, args: dict, idem: str) -> dict:
        route = "keypad_payment" if tool == "take_payment" and self.payment_mode == "keypad" else tool
        module = harness.ROUTES[route]
        req: dict[str, Any] = {"tool": tool, "route": f"/{route}", "signed": True, "scheme": "v1"}
        self.requests.append(req)
        signing = self.world.signing
        with self.world.patched(self.up, self.table):
            self.up.down = {SERVICE_OF[t] for t in self.down if t in SERVICE_OF}
            body, headers = tools.tool_request(args, idem)  # signed with the console's TOOL_API_SECRET
            event = {"body": body.decode(), "headers": headers}
            before = signing.verified
            started = time.perf_counter()
            try:
                resp = module.handler(event, None)
            except Exception as e:
                req.update(status_code=502, signature_ok=signing.verified > before)
                raise RuntimeError(f"502 from API Gateway: {route} Lambda raised {e}") from e
            finally:
                req["ms"] = round((time.perf_counter() - started) * 1000, 2)
        req.update(status_code=resp["statusCode"], signature_ok=signing.verified > before)
        req["headers"] = {k: (v[:12] + "…" if k == _common.SIGNATURE_HEADER else v) for k, v in headers.items()}
        payload = json.loads(resp["body"])
        if 400 <= resp["statusCode"] < 500:
            raise tools.ToolRejected(payload.get("error", "rejected"))
        if resp["statusCode"] >= 500:
            raise RuntimeError(f"{resp['statusCode']} from {route}")
        if payload.get("status") != "duplicate":
            self.received.append({"tool": tool, "payload": json.loads(body)})
        if route == "keypad_payment" and self.up.keypad_sessions:
            self.pay_twiml = self.up.keypad_sessions[-1]["twiml"]
        return payload

    def play_call_start(self, form: dict, cfg) -> list[str]:
        """The real voice and consent webhook Lambdas, with requests signed the way Twilio signs them."""
        env = {
            "AGENT_PUBLIC_WS_URL": DEMO_STREAM_URL,
            "RECORDING_ENABLED": "true" if cfg.recording_enabled else "false",
            "RECORDING_CONSENT_MODE": cfg.consent_mode,
        }
        voice_form = {k: v for k, v in form.items() if k != "Digits"}
        with self.world.patched(self.up, self.table), patch.dict(os.environ, env):
            first = harness._twilio_post(twilio_voice_hook.handler, "/voice", voice_form)
            played = [first]
            if "<Gather" in first:
                played.append(harness._twilio_post(twilio_consent.handler, "/consent", dict(form)))
        self.up.call_start_twiml.extend(played)
        return played

    def recording_client(self):
        return self.up.twilio_client()


def signing_selftest(world: LiveWorld) -> list[dict]:
    """Send the real take_payment handler good and bad requests; it must refuse every bad one with 401."""
    from src.handlers import take_payment

    up, table = harness.Upstreams(), harness.MemoryTable()
    args = {"amount_usd": 1, "description": "Signing self-test", "customer_email": "selftest@example.com"}
    rows: list[dict] = []
    first: dict = {}

    def send(case: str, expect: int, mutate=None, replay: bool = False) -> None:
        with world.patched(up, table):
            if replay:
                event = json.loads(json.dumps(first))
            else:
                body, headers = tools.tool_request(args, f"selftest-{secrets.token_hex(6)}")
                event = {"body": body.decode(), "headers": dict(headers)}
                if not first:
                    first.update(json.loads(json.dumps(event)))
                if mutate:
                    event = mutate(event)
            resp = take_payment.handler(event, None)
        answer = json.loads(resp["body"])
        got = resp["statusCode"]
        rows.append(
            {
                "case": case,
                "expected": expect,
                "got": got,
                "answer": answer.get("status") or answer.get("error"),
                "ok": got == expect,
            }
        )

    def unsigned(ev):
        ev["headers"] = {"Idempotency-Key": ev["headers"]["Idempotency-Key"]}
        return ev

    def altered(ev):
        ev["body"] = ev["body"].replace('"amount_usd":1,', '"amount_usd":4999,')
        return ev

    def stale(ev):
        key = ev["headers"]["Idempotency-Key"]
        ev["headers"] = _common.signed_headers(
            world.signing.env["TOOL_API_SECRET"], key, ev["body"].encode(), now=time.time() - 600
        )
        return ev

    def rekeyed(ev):
        ev["headers"]["Idempotency-Key"] = "a-different-key"
        return ev

    send("signed request", 200)
    send("no signature", 401, unsigned)
    send("body altered after signing", 401, altered)
    send("signed 10 minutes ago", 401, stale)
    send("idempotency key swapped after signing", 401, rekeyed)
    send("first request replayed as-is", 200, replay=True)
    return rows


def run_live_evals(console: Console) -> dict:
    """The real eval harness and simulated callers, against the console's applied policy."""
    policy = console.loaded
    started = time.time()
    scen = harness.run_all(policy=policy)
    personas = simulate.run_all(policy=policy)
    console.live_eval = {
        "ran_at": started,
        "seconds": round(time.time() - started, 2),
        "policy_ref": console.policy_ref,
        "call_evals": json.loads(harness.results_json(scen)),
        "simulated_callers": json.loads(simulate.results_json(personas)),
    }
    for p in console.live_eval["simulated_callers"]["personas"]:
        p.pop("transcript", None)
    return console.live_eval


def _env_status():
    """Which provider and deployment settings are present in this process (names only, never values)."""

    def extra() -> dict:
        from src.agent import provider

        names = set()
        for cls in provider._REGISTRY.values():
            names |= set(_env_names(cls))
        return {
            "agent_provider": os.environ.get("AGENT_PROVIDER", "openai_realtime"),
            "agent_provider_set": bool(os.environ.get("AGENT_PROVIDER")),
            "env_present": {n: bool(os.environ.get(n)) for n in sorted(names)},
            "lambda_base_url": "set" if os.environ.get("LAMBDA_BASE_URL") else "not set (handlers run in-process)",
            "payment_mode_env": os.environ.get("PAYMENT_MODE", "link"),
            "policy_file": str(POLICY_FILE.relative_to(ROOT)),
        }

    return extra


def _env_names(cls) -> list[str]:
    import inspect
    import re

    try:
        src = inspect.getsource(cls.build_services)
    except (OSError, TypeError):
        return []
    return re.findall(r'os\.environ(?:\.get\(|\[)"(\w+)"', src)


def build_console(world: LiveWorld) -> Console:
    eval_data = json.loads(EVAL_DATA.read_text()) if EVAL_DATA.exists() else {}
    return Console(
        policy_text=POLICY_FILE.read_text(),
        policy_source=str(POLICY_FILE.relative_to(ROOT)),
        personas_text=PERSONAS_FILE.read_text(),
        eval_data=eval_data,
        backend_factory=lambda: LambdaBackends(world),
        mode="live",
        settings_extra=_env_status(),
    )


def create_app(token: str | None = None, *, seed: bool = True, world: LiveWorld | None = None) -> FastAPI:
    """The console app. `token` (default: CONSOLE_TOKEN) protects /api/*; `seed` plays the simulated callers once."""
    token = os.environ.get("CONSOLE_TOKEN", "") if token is None else token
    world = world or LiveWorld()
    console = build_console(world)
    if seed:
        with world.lock:
            console.run_personas()
    app = FastAPI(title="secure-voice-agent console", version=VERSION)
    app.state.console = console
    app.state.world = world

    def require_token(request: Request) -> None:
        if not token:
            return
        given = request.headers.get("authorization", "")
        given = given[7:] if given.lower().startswith("bearer ") else request.headers.get("x-console-token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="console token required (Settings → Console token)")

    def run(cmd: str, payload: dict | None = None) -> Any:
        with world.lock:
            try:
                return dispatch(console, cmd, payload or {})
            except KeyError as e:
                raise HTTPException(status_code=404, detail=str(e.args[0]) if e.args else "not found") from e
            except (ValueError, TypeError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

    async def body(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="body must be JSON") from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        return data

    guard = [Depends(require_token)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": VERSION}

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/console/")

    @app.get("/console/api-mode")
    def api_mode(request: Request) -> dict:
        return {
            "mode": "live",
            "host": request.headers.get("host", socket.gethostname()),
            "version": VERSION,
            "auth_required": bool(token),
            "api": "/api",
        }

    # ---- read-only listings ----

    @app.get("/api/info", dependencies=guard)
    def info() -> Any:
        return run("info")

    @app.get("/api/overview", dependencies=guard)
    def overview() -> Any:
        return run("overview")

    @app.get("/api/calls", dependencies=guard)
    def calls() -> Any:
        return run("calls_list")

    @app.get("/api/calls/{call_id}", dependencies=guard)
    def call_detail(call_id: str) -> Any:
        return run("call_detail", {"call_id": call_id})

    @app.get("/api/scenarios", dependencies=guard)
    def scenarios() -> Any:
        return run("scenarios")

    @app.get("/api/personas", dependencies=guard)
    def personas() -> Any:
        return run("persona_list")

    @app.get("/api/policy", dependencies=guard)
    def policy() -> Any:
        return run("policy_get")

    @app.get("/api/evals", dependencies=guard)
    def evals() -> Any:
        return run("evals")

    @app.get("/api/settings", dependencies=guard)
    def settings() -> Any:
        return run("settings")

    # ---- actions ----

    @app.post("/api/calls", dependencies=guard)
    async def new_call(request: Request) -> Any:
        return run("new_call", {"opts": await body(request)})

    call_actions = {
        "say": ("text", "auto_run"),
        "tool": ("tool", "args"),
        "run_pending": ("index", "args"),
        "skip_pending": ("index",),
        "advance": ("seconds",),
        "payment_mode": ("mode",),
        "sim_swap": ("on",),
        "keypad_result": ("result",),
        "end_call": (),
        "audit_verify": (),
        "audit_tamper": ("line",),
        "audit_undo": (),
    }

    @app.post("/api/calls/{call_id}/{action}", dependencies=guard)
    async def call_action(call_id: str, action: str, request: Request) -> Any:
        if action not in call_actions:
            raise HTTPException(status_code=404, detail=f"unknown action {action!r}")
        data = await body(request)
        return run(action, {"call_id": call_id, **{k: data[k] for k in call_actions[action] if k in data}})

    @app.post("/api/scenarios/{scenario_id}/run", dependencies=guard)
    def run_scenario(scenario_id: str) -> Any:
        return run("run_scenario", {"scenario_id": scenario_id})

    @app.post("/api/personas/{persona_id}/run", dependencies=guard)
    def run_persona(persona_id: str) -> Any:
        return run("run_persona", {"persona_id": persona_id})

    @app.post("/api/personas/run", dependencies=guard)
    async def run_personas(request: Request) -> Any:
        data = await body(request)
        return run("run_personas", {"ids": data.get("ids")} if data.get("ids") else {})

    @app.post("/api/policy/validate", dependencies=guard)
    async def policy_validate(request: Request) -> Any:
        return run("policy_validate", {"text": str((await body(request)).get("text", ""))})

    @app.put("/api/policy", dependencies=guard)
    async def policy_apply(request: Request) -> Any:
        return run("policy_apply", {"text": str((await body(request)).get("text", ""))})

    @app.delete("/api/policy", dependencies=guard)
    def policy_reset() -> Any:
        return run("policy_reset")

    @app.post("/api/policy/compare", dependencies=guard)
    async def policy_compare(request: Request) -> Any:
        return run("policy_compare", {"target": str((await body(request)).get("target", ""))})

    @app.post("/api/evals/run", dependencies=guard)
    def evals_run() -> Any:
        with world.lock:
            return run_live_evals(console)

    @app.post("/api/signing/selftest", dependencies=guard)
    def selftest() -> Any:
        with world.lock:
            return {"cases": signing_selftest(world), "signing": world.signing.status()}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # The static console last, so /console/api-mode above wins over demo/api-mode (the demo-mode answer).
    app.mount("/console", StaticFiles(directory=DEMO_DIR, html=True), name="console")
    return app


def main() -> None:  # pragma: no cover - runs a server
    import uvicorn

    host = os.environ.get("CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("CONSOLE_PORT", "8090"))
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"Secure Voice Agent console (live mode): http://{shown}:{port}/console/")
    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":  # pragma: no cover
    main()
