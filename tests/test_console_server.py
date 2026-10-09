# Corey Mathie, 2026
"""
The local console server (src/console_server.py): live mode serves the console
and backs it with the real Lambda handler code behind HMAC-signed requests.
"""

import json

import pytest
from fastapi.testclient import TestClient

from evals import simulate
from src.console_server import LiveWorld, Signing, create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(token=""))


def test_api_mode_and_static_console(client):
    meta = client.get("/console/api-mode").json()
    assert meta["mode"] == "live" and meta["auth_required"] is False and meta["version"] == "0.7.0"
    page = client.get("/console/")
    assert page.status_code == 200 and "Secure Voice Agent" in page.text
    assert client.get("/console/app.js").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/console/"
    assert client.get("/health").json()["status"] == "ok"


def test_seeded_simulated_callers_match_the_eval_harness(client):
    calls = [c for c in client.get("/api/calls").json() if c["source"] == "persona"]
    reference = {r.id: r for r in simulate.run_all()}
    assert len(calls) == len(reference) == 14
    for c in calls:
        assert c["statuses"] == reference[c["ref"]].tool_statuses, c["ref"]
        assert c["persona"]["controls"] == reference[c["ref"]].controls, c["ref"]


def test_a_live_call_signs_every_tool_request(client):
    d = client.post("/api/calls", json={"state": "TX", "recording_enabled": True}).json()
    cid = d["id"]
    assert d["call_start"]["audit"]["recording"] == "started" and "not a person" in d["call_start"]["twiml"][0]
    d = client.post(
        f"/api/calls/{cid}/say",
        json={"text": "I'd like to make my personal loan payment, it's $180. Email lee@example.com"},
    ).json()
    code = d["phone"]["messages"][-1]["code"]
    d = client.post(f"/api/calls/{cid}/say", json={"text": f"it's {code}"}).json()
    payment = d["tool_calls"][-1]
    assert payment["result"]["status"] == "link_sent"
    (req,) = payment["requests"]
    assert req["signed"] and req["signature_ok"] and req["status_code"] == 200 and req["route"] == "/take_payment"
    assert "request signing" in [s["stage"] for s in payment["stages"]]
    assert req["headers"]["X-Tool-Signature"].endswith("…")  # truncated in responses
    assert d["verify"]["ok"]


def test_keypad_mode_runs_the_keypad_lambda(client):
    opts = {"state": "FL", "digits": "1", "payment_mode": "keypad", "start_verified": True}
    cid = client.post("/api/calls", json=opts).json()["id"]
    d = client.post(
        f"/api/calls/{cid}/say", json={"text": "I want to pay $120 for my personal loan, email ana@example.com"}
    ).json()
    t = d["tool_calls"][-1]
    assert t["result"]["status"] == "keypad_started" and t["requests"][0]["route"] == "/keypad_payment"
    assert 'chargeAmount="120.00"' in d["keypad"]["pay_twiml"] and d["keypad"]["capture"]["recording_paused"]
    d = client.post(f"/api/calls/{cid}/keypad_result", json={"result": "success"}).json()
    assert 'value="success"' in d["keypad"]["resume_twiml"]


def test_scenarios_personas_and_audit_endpoints(client):
    d = client.post("/api/scenarios/card-in-ticket/run").json()
    body = d["tool_calls"][0]["sent_to_backend"]["body"]
    assert "[REDACTED_PAN]" in body and "4111" not in json.dumps(d["audit"])
    cid = d["id"]
    t = client.post(f"/api/calls/{cid}/audit_tamper", json={"line": 1}).json()
    assert t["verify"]["bad_line"] == 1
    assert client.post(f"/api/calls/{cid}/audit_undo").json()["verify"]["ok"]
    p = client.post("/api/personas/se-spoofed-caller-id/run").json()["persona"]
    assert p["correct"] and not p["achieved"] and "step_up_locked" in p["controls"]
    assert len(client.get("/api/scenarios").json()) == 12 and len(client.get("/api/personas").json()) == 14


def test_policy_endpoints(client):
    pol = client.get("/api/policy").json()
    assert pol["modified"] is False and pol["applied"]["ref"] == pol["file_ref"]
    bad = client.post("/api/policy/validate", json={"text": "version: 2"}).json()
    assert bad["ok"] is False and any("unsupported policy version" in e for e in bad["errors"])
    weak = pol["text"].replace("  threshold: 4", "  threshold: 1", 1)
    assert client.put("/api/policy", json={"text": weak}).json()["applied_ok"]
    cmp = client.post("/api/policy/compare", json={"target": "urgent-but-benign"})
    assert cmp.status_code == 404  # not a console scenario or persona id
    cmp = client.post("/api/policy/compare", json={"target": "benign-urgency"}).json()
    assert cmp["shipped"]["statuses"] == ["link_sent"] and cmp["applied"]["statuses"] == ["require_human"]
    assert client.delete("/api/policy").json()["modified"] is False


def test_evals_rerun_on_the_server(client):
    out = client.post("/api/evals/run").json()
    assert out["call_evals"]["passed"] == out["call_evals"]["total"] == 31
    assert out["simulated_callers"]["metrics"]["expectations_met"] == 14
    assert client.get("/api/evals").json()["live"]["policy_ref"] == out["policy_ref"]


def test_signing_selftest_refuses_every_tampered_request(client):
    out = client.post("/api/signing/selftest").json()
    assert all(c["ok"] for c in out["cases"]) and len(out["cases"]) == 6
    assert [c["got"] for c in out["cases"]] == [200, 401, 401, 401, 401, 200]
    assert out["cases"][-1]["answer"] == "duplicate"
    assert out["signing"]["rejected"] >= 4 and out["signing"]["secret_source"] == "generated at startup"


def test_secrets_never_leave_the_server():
    signing = Signing(secret="super-secret-value-for-the-test")
    client = TestClient(create_app(token="", seed=False, world=LiveWorld(signing)))
    cid = client.post("/api/calls", json={"start_verified": True}).json()["id"]
    client.post(f"/api/calls/{cid}/say", json={"text": "Pay $50 on my credit card please, email a@example.com"})
    seen = "".join(
        client.get(path).text
        for path in ("/api/settings", "/api/calls", f"/api/calls/{cid}", "/api/overview", "/api/info", "/api/evals")
    )
    seen += client.post("/api/signing/selftest").text
    assert "super-secret-value-for-the-test" not in seen
    assert client.get("/api/settings").json()["signing"]["secret_source"] == "TOOL_API_SECRET from the environment"


def test_settings_report_env_presence_not_values(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    s = client.get("/api/settings").json()
    assert s["env_present"]["OPENAI_API_KEY"] is True and "sk-test-not-a-real-key" not in json.dumps(s)
    assert s["signing"]["enabled"] and s["signing"]["secret"] == "configured (never displayed)"


def test_console_token_is_required_when_set():
    client = TestClient(create_app(token="s3cret", seed=False))
    assert client.get("/console/api-mode").json()["auth_required"] is True
    assert client.get("/api/info").status_code == 401
    assert client.get("/api/info", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/info", headers={"X-Console-Token": "s3cret"}).status_code == 200
    assert client.get("/console/").status_code == 200  # the static page itself is public


def test_errors_are_json(client):
    assert client.get("/api/calls/nope").json() == {"error": "no call 'nope'"}
    cid = client.post("/api/calls", json={}).json()["id"]
    assert client.post(f"/api/calls/{cid}/rm").status_code == 404
    assert client.post(f"/api/calls/{cid}/say", content=b"not json").status_code == 400
    client.post(f"/api/calls/{cid}/end_call")
    r = client.post(f"/api/calls/{cid}/say", json={"text": "hello"})
    assert r.status_code == 400 and "ended" in r.json()["error"]
