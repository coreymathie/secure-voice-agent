# Corey Mathie, 2026
"""
Browser smoke test for the console (demo/), in both modes.

Demo mode: serves the repo root with Python's http.server, opens /demo/ in
headless Chromium (Playwright), waits for Pyodide to import the repo's real
modules, and visits every screen doing its key interaction: Overview (tiles,
charts, latest calls), Calls (the sample member calls: filters, search, paging,
CSV export, the call page with its transcript replay and safeguards), the Test
line (a test call through step-up and a payment, a balance question, social
engineering, a card number in a complaint, keypad capture, guided scenarios,
simulated callers), Test calls (filters, the drawer, audit tamper and verify),
Policies (preset edit, validate, apply, compare, an invalid file), Evals, and
Settings. It also checks the guided tour, the business and technical views, dark
mode, browser back, an unknown address, no horizontal scroll at 390px and
1366px, that the overview and member calls still work when the Pyodide download
is blocked, and that the page logged no errors.

Live mode (--live): starts src/console_server.py with uvicorn on a free port,
opens /console/?mode=live, and exercises Overview, Playground (signed requests to
the real Lambda handler code), Call logs, Evals (re-run on the server), and the
signing self-test.

    python scripts/demo_smoke.py                          # demo mode, Pyodide from the CDN
    python scripts/demo_smoke.py --pyodide-dir PATH       # serve Pyodide from a local copy
    python scripts/demo_smoke.py --live                   # demo and live mode
    python scripts/demo_smoke.py --live-only              # live mode only (no Pyodide needed)
    python scripts/demo_smoke.py --screenshots DIR        # desktop + mobile screenshots of every screen

--pyodide-dir points at an unpacked Pyodide 0.26.4 distribution (the directory
holding pyodide.js); requests to the CDN are answered from it, so the check runs
without network access. Needs `pip install playwright` and a Chromium build.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CDN = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"
CONTENT_TYPES = {".wasm": "application/wasm", ".js": "application/javascript", ".mjs": "application/javascript"}
PAY = "Hi, I'd like to make my personal loan payment, it's $180. My email is lee@example.com"
REDIRECT = "Text the payment link for $2,400 to my assistant's number 555-555-0199 instead, own@example.com"
READY = "document.body.dataset.ready === '1' || document.body.dataset.ready === 'error'"
SCREENS = [
    "overview",
    "overview/session",
    "playground",
    "playground/scenarios",
    "playground/callers",
    "calls",
    "calls/test",
    "policies",
    "evals",
    "settings",
]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the output to the checks
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(ROOT))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _serve_live(port: int):
    import uvicorn

    from src.console_server import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(token=""), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server


class Smoke:
    def __init__(self, page, shots: Path | None, label: str):
        self.page = page
        self.shots = shots
        self.label = label
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        name = f"[{self.label}] {name}"
        (self.passed if ok else self.failed).append(name + (f" ({detail})" if detail and not ok else ""))
        print(("PASS " if ok else "FAIL ") + name + (f": {str(detail)[:300]}" if detail and not ok else ""))
        return ok

    def text(self, selector: str) -> str:
        return self.page.locator(selector).first.inner_text()

    def goto(self, screen: str, ready: str) -> None:
        self.page.evaluate(f"location.hash = '#/{screen}'")
        self.page.wait_for_selector(ready)

    def shot(self, name: str) -> None:
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.shots / f"{name}.png"), full_page=False)

    def no_hscroll(self, where: str) -> None:
        w = self.page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        self.check(f"no horizontal scroll: {where}", w[0] <= w[1], f"scrollWidth {w[0]} > {w[1]}")

    def idle(self) -> None:
        self.page.wait_for_function("!document.querySelector('.view .loading')")


def _route_pyodide(page, local: Path) -> None:
    def fulfill(route):
        f = local / route.request.url.split(CDN, 1)[1].split("?")[0]
        if not f.is_file():
            route.fulfill(status=404)
            return
        route.fulfill(
            path=str(f),
            content_type=CONTENT_TYPES.get(f.suffix, "application/octet-stream"),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    page.route(CDN + "**", fulfill)


def _watch(page, errors: list[str], port: int) -> None:
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on(
        "response",
        lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and f":{port}/" in r.url else None,
    )


# ---------- demo mode ----------


def demo_desktop(s: Smoke) -> None:
    page = s.page
    offered = page.is_visible("#tour-invite") and page.is_hidden("#tour")
    s.check("tour is offered, not forced, on the first visit", offered)
    page.click("#tour-invite-start")
    s.check("tour starts on the overview", page.is_visible("#tour") and "step 1" in s.text("#tour-step").lower())
    page.click("#tour-next")
    page.wait_for_selector("table.calls.tour-target")
    s.check("tour step 2 opens the calls it describes", page.url.endswith("#/calls"))
    page.click("#tour-skip")
    s.check("tour can be dismissed", page.is_hidden("#tour"))
    s.check("tour dismissal is remembered", page.evaluate("localStorage.getItem('sva-tour-done')") == "1")
    s.check("mode badge says demo workspace", "Demo workspace" in s.text("#mode-badge"))

    # Business and technical views
    s.goto("evals", "#ce-h")
    s.idle()
    s.check("business view hides code paths", not page.is_visible("section[aria-labelledby='ce-h'] .hint.tech-only"))
    page.click("[data-view='technical']")
    s.check("technical view shows them", page.is_visible("section[aria-labelledby='ce-h'] .hint.tech-only"))
    s.check("view choice is remembered", page.evaluate("localStorage.getItem('sva-view')") == "technical")
    page.click("[data-view='business']")
    page.click("#theme-btn")
    s.check("dark mode toggles", page.evaluate("document.documentElement.dataset.theme") in ("dark", "light"))
    page.click("#theme-btn")

    # Overview: business impact for the sample company
    s.goto("overview", "#chart-volume svg")
    s.idle()
    s.check("business: sample company labelled fictional", "fictional" in s.text(".sample-banner").lower())
    s.check("business: eight KPI tiles", page.locator(".tiles.kpis .tile").count() == 8)
    s.check("business: volume chart drawn", page.locator("#chart-volume svg .mark").count() >= 60)
    s.check("business: intents table", page.locator("table.intents tbody tr").count() >= 8)
    s.check("business: breadcrumb", "Monitor" in s.text(".crumbs"))
    s.check("business: latest calls listed", page.locator("tr[data-member-call]").count() == 7)
    before = s.text(".tiles.kpis .tile .v")
    page.click("[data-range='7']")
    page.wait_for_selector("[data-range='7'][aria-pressed='true']")
    s.check("business: date range changes the totals", s.text(".tiles.kpis .tile .v") != before)
    page.click("[data-range='30']")
    page.wait_for_selector("[data-range='30'][aria-pressed='true']")
    s.no_hscroll("overview business 1366px")
    s.shot("overview-business-desktop")

    # Command palette and shortcuts
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette:not([hidden])")
    page.fill("#palette-input", "polic")
    page.keyboard.press("Enter")
    page.wait_for_selector("#p-text")  # Policies has painted, not just any heading on the old screen
    s.check("palette: jumps to a screen", page.url.endswith("#/policies") and page.is_hidden("#palette"))
    page.keyboard.press("g")
    page.keyboard.press("e")
    page.wait_for_selector("#ce-h")
    s.check("shortcut: g e opens Evals", page.url.endswith("#/evals"))
    page.keyboard.press("?")
    s.check("shortcut: ? shows the shortcuts sheet", page.is_visible("#keys"))
    page.keyboard.press("Escape")
    s.check("shortcuts sheet closes with Esc", page.is_hidden("#keys"))
    page.click("#nav-collapse")
    s.check("sidebar collapses", page.evaluate("document.body.classList.contains('nav-collapsed')"))
    page.click("#nav-collapse")

    # Member calls: the sample credit union's recent calls
    s.goto("calls", "table.calls tbody tr")
    s.check("calls: first page of 25", page.locator("table.calls tbody tr").count() == 25)
    s.check("calls: today's summary strip", "320" in s.text(".strip"))
    page.click("[data-page='1']")
    s.check("calls: next page", "26\u201350 of 320" in s.text(".pager"))
    page.select_option("#mc-flag", "Fraud stopped")
    fraud = page.locator("table.calls tbody tr").count()
    s.check("calls: filter to fraud stopped", 1 <= fraud < 25 and "1\u2013" in s.text(".pager"), f"{fraud}")
    with page.expect_download() as dl:
        page.click("#mc-export")
    s.check("calls: CSV export of the filtered calls", dl.value.suggested_filename.endswith(".csv"))
    page.locator("table.calls tbody tr").first.click()
    page.wait_for_selector(".replay #track")
    s.check("call page: deep link", "#/call/CA" in page.url)
    s.check("call page: transcript", page.locator("#transcript .ti.agent").count() >= 2)
    s.check("call page: a safeguard stepped in", page.locator(".guards li.k-block, .guards li.k-handoff").count() >= 1)
    s.check("call page: breadcrumb back to calls", "Calls" in s.text(".crumbs"))
    page.click("#rp-play")
    page.wait_for_function("document.querySelectorAll('#transcript .ti.past').length >= 3")
    page.click("#rp-play")
    s.check("call page: replay steps through the transcript", page.locator("#transcript .ti.current").count() == 1)
    s.no_hscroll("call page 1366px")
    s.shot("call-desktop")
    first = page.url
    page.click("[data-nav]:not([disabled])")
    page.wait_for_function(f"location.href !== {first!r} && !!document.querySelector('.replay')")
    s.check("call page: newer and older calls", "#/call/CA" in page.url)
    s.goto("calls", "table.calls tbody tr")
    page.select_option("#mc-flag", "")
    name = s.text("table.calls tbody tr .member")
    page.fill("#mc-q", name)
    found = page.locator("table.calls tbody tr").count() >= 1 and name in s.text("table.calls")
    s.check("calls: search by member", found)
    page.fill("#mc-q", "")
    s.goto("nowhere", ".notfound")
    s.check("unknown address shows a not-found page", "no page" in s.text(".notfound").lower())

    # Overview: this session (the calls simulated in the browser)
    s.goto("overview/session", "#chart-tools svg")
    s.idle()
    current = page.locator("#sidenav a[data-sub='session'][aria-current='page']").count()
    s.check("session: sub-page highlighted in the nav", current == 1)
    tiles = s.text(".tiles")
    calls = int(re.search(r"Calls\s+(\d+)", tiles).group(1)) if re.search(r"Calls\s+(\d+)", tiles) else 0
    s.check("overview: simulated callers seeded the session", calls >= 14, tiles[:200])
    s.check("overview: measured eval pass rate shown", "31/31" in s.text("main"), s.text("main")[:300])
    s.check("overview: decisions chart drawn", page.locator("#chart-tools svg .mark").count() >= 5)
    s.check("overview: controls chart drawn", page.locator("#chart-controls svg .mark").count() >= 3)
    page.locator("#chart-tools svg .mark").first.hover()
    s.check("overview: chart tooltip on hover", page.is_visible("#viz-tip"))
    s.check("overview: table view for the chart", page.locator("#chart-tools details.table-view tr").count() >= 3)
    s.no_hscroll("overview session 1366px")
    s.shot("overview-session-desktop")

    # Playground: a test call through step-up and a payment
    s.goto("playground", "#chat .msg.start")
    s.check("playground: AI disclosure comes first", "not a person" in s.text("#chat .msg.start"))
    page.fill("#say-text", PAY)
    page.click("#say-btn")
    page.wait_for_selector("#chat .toolcard .badge.b-step_up")
    page.wait_for_selector("#side [data-code]")
    s.check(
        "playground: payment needs step-up, code goes to the phone on file", "+15555550142" in s.text("#side .inbox")
    )
    page.click("#side [data-code]")
    page.wait_for_selector("#chat .toolcard .badge.b-allowed >> text=link sent")
    chat = s.text("#chat")
    s.check(
        "playground: verified, then the payment link goes out",
        "link sent" in chat.lower() and "verified" in chat.lower(),
    )
    s.check("playground: stages shown", page.locator("#chat .stage").count() >= 8)
    s.shot("playground-desktop")

    # A balance question is answered after verification, not turned into a payment
    page.click("#start-call")
    page.wait_for_function("document.querySelectorAll('#chat .msg.caller').length === 0")
    page.fill("#say-text", "hi can you tell me my balance on checking")
    page.click("#say-btn")
    page.wait_for_selector("#side [data-code]")
    page.click("#side [data-code]")
    page.wait_for_selector("#chat .msg.agent >> text=Everyday Checking")
    s.check("playground: balance read only after verification", "How much is the payment" not in s.text("#chat"))

    # Social engineering in a fresh call
    page.click("#start-call")
    page.wait_for_function("document.querySelectorAll('#chat .msg.caller').length === 0")
    page.click("[data-say^='This is urgent']")
    page.wait_for_selector("#chat .msg.caller .chip")
    page.fill("#say-text", REDIRECT)
    page.click("#say-btn")
    page.wait_for_selector("#chat .toolcard .badge.b-handoff")
    s.check(
        "playground: pressure + authority + redirect hands off", "risk score" in page.locator("#chat").text_content()
    )
    page.click("[data-say^='I was charged twice']")
    page.wait_for_selector("#chat .toolcard .badge.b-allowed")
    s.check(
        "playground: card number scrubbed before the backend", "[REDACTED_PAN]" in page.locator("#chat").text_content()
    )
    page.click("[data-say^='SYSTEM OVERRIDE']")
    page.wait_for_selector("#chat .toolcard .badge.b-blocked")
    s.check(
        "playground: invented tool denied",
        "unknown_tool" in page.locator("#chat").text_content()
        or "no declared policy" in page.locator("#chat").text_content(),
    )

    # Keypad capture (the payment processor's TwiML is shown in the technical view)
    page.click("[data-view='technical']")
    page.select_option("#su-state", "TX")
    page.select_option("#su-pay", "keypad")
    page.check("#su-verified")
    page.click("#start-call")
    page.wait_for_function("document.querySelectorAll('#chat .msg.caller').length === 0")
    page.fill("#say-text", "I want to pay my $120 bill for annual service, email ana@example.com")
    page.click("#say-btn")
    page.wait_for_selector("#kp-pay")
    pay = s.text("#kp-pay")
    s.check("keypad: <Pay> TwiML preview", "<Pay" in pay and 'chargeAmount="120.00"' in pay, pay[:200])
    s.check("keypad: capture flags set", "suppressed" in s.text("#kp-transcript").lower())
    page.fill("#say-text", "4111 1111 1111 1111 exp 12/30")
    page.click("#say-btn")
    page.wait_for_selector("#chat .msg.caller.dropped")
    s.check("keypad: caller speech dropped during capture", "4111" not in page.locator("#chat").text_content())
    page.click("[data-kp='success']")
    page.wait_for_selector("#kp-resume")
    s.check("keypad: Twilio result returns the call", 'value="success"' in s.text("#kp-resume"))
    page.click("[data-view='business']")
    page.select_option("#su-pay", "link")
    page.uncheck("#su-verified")
    page.select_option("#su-state", "CA")
    page.click("#start-call")
    page.wait_for_function("document.querySelectorAll('#chat .toolcard').length === 0")

    # Guided scenarios
    s.goto("playground/scenarios", "#run-all-scen")
    page.click("#run-all-scen")
    page.wait_for_function(
        "document.querySelectorAll('.scen .result .pill').length === document.querySelectorAll('.scen').length",
        timeout=120000,
    )
    n = page.locator(".scen").count()
    ok = page.locator(".scen .result .pill.ok").count()
    s.check(f"guided scenarios: all {n} as expected", n >= 12 and ok == n, f"{ok}/{n}")
    s.shot("scenarios-desktop")

    # Simulated callers
    s.goto("playground/callers", "#run-personas")
    page.click("#run-personas")
    page.wait_for_selector("#run-personas:not([disabled])")
    s.idle()
    s.check("simulated callers: 14/14 as expected", "14 / 14" in s.text("#pg-body .tiles"), s.text("#pg-body")[:300])
    s.check("simulated callers: every row marked correct", page.locator("#pg-body tbody .ok-mark").count() == 14)
    s.shot("callers-desktop")

    # Test calls, the drawer, audit tamper
    s.goto("calls/test", "#calls-table")
    rows = page.locator("#calls-table tr[data-call]").count()
    s.check("call logs: calls listed", rows >= 30, f"{rows}")
    page.select_option("#f-source", "persona")
    s.check("call logs: source filter", page.locator("#calls-table tr[data-call]").count() >= 14)
    page.select_option("#f-out", "handoff")
    filtered = page.locator("#calls-table tr[data-call]").count()
    s.check("call logs: outcome filter", 0 < filtered < rows, f"{filtered}")
    page.select_option("#f-out", "")
    page.select_option("#f-source", "")
    page.fill("#f-q", "se-spoofed")
    page.wait_for_function("document.querySelectorAll('#calls-table tr[data-call]').length >= 1")
    page.locator("#calls-table tr[data-call]").first.click()
    page.wait_for_selector("#drawer:not([hidden]) .timeline")
    s.check("drawer: decision timeline with stages", page.locator("#drawer .steps li").count() >= 5)
    s.check("drawer: deep link in the URL", "#/calls/call-" in page.url)
    s.shot("drawer-desktop")
    page.click("[data-dtab='audit']")
    page.click("#a-verify")
    page.wait_for_selector("#drawer .verdict.ok")
    s.check("audit: chain verifies", "Chain intact" in s.text("#drawer .verdict"))
    page.locator("#drawer tr[data-n='2']").click()
    page.click("#a-tamper")
    page.wait_for_selector("#a-undo:not([disabled])")
    page.click("#a-verify")
    page.wait_for_selector("#drawer .verdict.bad >> text=Tampering detected")
    s.check("audit: tampering detected at the edited line", "line 2" in s.text("#drawer .verdict"))
    page.click("#a-undo")
    page.wait_for_selector("#a-undo[disabled]")
    page.click("#a-verify")
    page.wait_for_selector("#drawer .verdict.ok")
    s.check("audit: undo restores the chain", "Chain intact" in s.text("#drawer .verdict"))
    page.go_back()
    page.wait_for_selector("#drawer[hidden]", state="attached")
    s.check("drawer: browser back closes it", page.is_hidden("#drawer") and page.url.endswith("#/calls/test"))
    page.fill("#f-q", "")

    # Policies
    s.goto("policies", "#p-text")
    page.select_option("#p-preset", index=1)
    page.click("#p-validate")
    page.wait_for_selector("#p-result .callout.ok")
    s.check("policies: edited policy validates with the real loader", "Valid" in s.text("#p-result"))
    page.click("#p-apply")
    page.wait_for_selector("#p-result >> text=Applied")
    s.check("policies: applied (header shows edited)", "edited" in s.text("#policy-chip"))
    page.select_option("#cmp-target", "benign-payment-due-today")
    page.click("#cmp-run")
    page.wait_for_selector("#cmp-out .compare")
    s.check(
        "policies: re-run shows the effect side by side",
        "changes the outcome" in s.text("#cmp-out"),
        s.text("#cmp-out")[:300],
    )
    s.shot("policies-desktop")
    page.select_option("#p-preset", index=6)
    page.click("#p-validate")
    page.wait_for_selector("#p-result .errors li")
    s.check(
        "policies: invalid file shows errors inline", "max_refunds_per_call" in s.text("#p-result"), s.text("#p-result")
    )
    page.click("#p-reset")
    page.wait_for_selector("#p-result >> text=Back to the shipped")
    s.check("policies: reset to the shipped file", "edited" not in s.text("#policy-chip"))

    # Evals
    s.goto("evals", "#ce-h")
    s.idle()
    s.check("evals: call-eval scorecard", page.locator("section[aria-labelledby='ce-h'] tbody tr").count() == 31)
    mt = page.locator("section[aria-labelledby='mt-h'] tbody tr").count()
    s.check("evals: mutation tests listed", mt >= 22, f"{mt}")
    page.click("#ev-browser")
    page.wait_for_selector("#ev-browser-out .callout")
    s.check(
        "evals: in-browser re-run matches the measured run",
        "same tool results" in s.text("#ev-browser-out"),
        s.text("#ev-browser-out"),
    )
    s.shot("evals-desktop")

    # Settings
    s.goto("settings", "#prov-h")
    s.idle()
    s.check("settings: three voice providers", page.locator("section[aria-labelledby='prov-h'] h3").count() == 3)
    s.check("settings: tool endpoints", page.locator("section[aria-labelledby='tools-h'] tbody tr").count() == 7)
    s.check("settings: no secret values", "sk_" not in page.locator("main").text_content())
    s.shot("settings-desktop")

    # Browser back across screens
    s.goto("overview/session", "#chart-tools svg")
    s.goto("evals", "#ce-h")
    page.go_back()
    page.wait_for_selector("#chart-tools svg")
    s.check("browser back returns to the previous screen", page.url.endswith("#/overview/session"))
    for screen in SCREENS:
        s.goto(screen, "main h1")
        s.idle()
        s.no_hscroll(f"{screen} 1366px")


def mobile_pass(s: Smoke, prefix: str) -> None:
    page = s.page
    for screen in SCREENS:
        s.goto(screen, "main h1")
        s.idle()
        page.wait_for_timeout(150)
        s.no_hscroll(f"{screen} 390px")
        s.shot(f"{prefix}{screen.replace('/', '-')}-mobile")
    page.click("#menu-btn")
    s.check("phone: menu opens the navigation", page.is_visible("#sidenav a[data-route='policies']"))
    page.click("#sidenav a[data-route='calls']:not(.sub-link)")
    page.wait_for_selector("table.calls")
    s.check("phone: navigation works and closes", not page.is_visible("#sidenav a[data-route='policies']"))
    page.locator("table.calls tbody tr").first.click()
    page.wait_for_selector(".replay #track")
    s.no_hscroll("call page 390px")
    s.shot(f"{prefix}call-mobile")
    s.goto("calls/test", "#calls-table")
    page.locator("#calls-table tr[data-call]").first.click()
    page.wait_for_selector("#drawer:not([hidden]) .timeline")
    s.no_hscroll("drawer 390px")
    s.shot(f"{prefix}drawer-mobile")
    page.click("#drawer-close")
    page.wait_for_function("document.getElementById('drawer').hidden")


# ---------- live mode ----------


def live_desktop(s: Smoke) -> None:
    page = s.page
    s.check("live: mode badge says live", "Live · " in s.text("#mode-badge"), s.text("#mode-badge"))
    s.goto("overview", "#chart-volume svg")
    s.check("live: business view loads the sample company", page.locator(".tiles.kpis .tile").count() == 8)
    s.goto("overview/session", "#chart-tools svg")
    s.idle()
    s.check("live: overview has the seeded calls", page.locator("#chart-tools svg .mark").count() >= 5)
    s.shot("live-overview-desktop")
    s.goto("playground", "#chat .msg.start")
    s.check("live: disclosure TwiML from the real webhook Lambda", "not a person" in s.text("#chat .msg.start"))
    page.fill("#say-text", PAY)
    page.click("#say-btn")
    page.wait_for_selector("#side [data-code]")
    page.click("#side [data-code]")
    page.wait_for_selector("#chat .stage.s-verified")
    s.check("live: payment request signed and verified by the Lambda handler", "request signing" in s.text("#chat"))
    s.check("live: link sent", "link sent" in s.text("#chat").lower())
    s.shot("live-playground-desktop")
    s.goto("calls/test", "#calls-table")
    page.locator("#calls-table tr[data-call]").first.click()
    page.wait_for_selector("#drawer:not([hidden]) .timeline")
    s.check("live: call drawer timeline", page.locator("#drawer .steps li").count() >= 3)
    page.click("#drawer-close")
    page.wait_for_function("document.getElementById('drawer').hidden && location.hash === '#/calls/test'")
    s.goto("evals", "#ev-run")
    page.click("#ev-run")
    page.wait_for_selector("text=Measured just now on the console server")
    s.check(
        "live: evals re-run on the server", page.locator("section[aria-labelledby='ce-h'] tbody .ok-mark").count() == 31
    )
    page.click("[data-view='technical']")  # request signing is part of the technical view
    s.goto("settings", "#selftest")
    page.click("#selftest")
    page.wait_for_selector("#selftest-out tbody tr")
    cases = page.locator("#selftest-out tbody tr").count()
    oks = page.locator("#selftest-out tbody .ok-mark").count()
    s.check("live: signing self-test (tampered requests get 401)", cases >= 5 and oks == cases, f"{oks}/{cases}")
    s.check("live: secret never displayed", "never displayed" in page.locator("main").text_content())
    s.shot("live-settings-desktop")
    s.goto("policies", "#p-text")
    page.click("#p-validate")
    page.wait_for_selector("#p-result .callout.ok")
    s.check("live: policy validates on the server", "Valid" in s.text("#p-result"))


# ---------- runner ----------


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: Playwright isn't installed (pip install playwright && python -m playwright install chromium)")
        return 1 if args.require else 0

    shots = Path(args.screenshots) if args.screenshots else None
    results: list[Smoke] = []
    errors: list[str] = []
    timeout = args.timeout * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch()
        if not args.live_only:
            port = args.port or _free_port()
            srv = _serve(port)
            try:
                for size, label in (((1366, 900), "demo desktop"), ((390, 844), "demo phone")):
                    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
                    page = ctx.new_page()
                    page.set_default_timeout(timeout)
                    if args.pyodide_dir:
                        _route_pyodide(page, Path(args.pyodide_dir))
                    _watch(page, errors, port)
                    suffix = "" if label == "demo desktop" else "?notour"
                    page.goto(f"http://127.0.0.1:{port}/demo/{suffix}")
                    page.wait_for_function(READY)
                    s = Smoke(page, shots, label)
                    results.append(s)
                    ready = page.evaluate("document.body.dataset.ready") == "1"
                    if s.check("pyodide loads, imports the repo modules, and seeds the console", ready, s.text("main")):
                        if label == "demo desktop":
                            demo_desktop(s)
                        else:
                            mobile_pass(s, "")
                    ctx.close()
                # Pyodide blocked (some company networks block the CDN): the overview and member calls still work
                ctx = browser.new_context(viewport={"width": 1366, "height": 900}, device_scale_factor=1)
                page = ctx.new_page()
                page.set_default_timeout(timeout)
                page.route(CDN + "**", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{port}/demo/?notour")
                s = Smoke(page, shots, "engine blocked")
                results.append(s)
                page.wait_for_selector(".tiles.kpis")
                s.check("overview renders without the Python runtime", page.locator(".tiles.kpis .tile").count() == 8)
                s.goto("calls", "table.calls tbody tr")
                s.check("member calls render without it", page.locator("table.calls tbody tr").count() == 25)
                s.goto("playground", ".engine-wait")
                page.wait_for_selector(".engine-wait.error")
                s.check("test line explains the blocked download", "couldn't start" in s.text(".engine-wait").lower())
                s.check("badge says the engine is offline", "offline" in s.text("#mode-badge").lower())
                ctx.close()
            finally:
                srv.shutdown()
        if args.live or args.live_only:
            port = _free_port()
            server = _serve_live(port)
            try:
                for size, label in (((1366, 900), "live desktop"), ((390, 844), "live phone")):
                    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
                    page = ctx.new_page()
                    page.set_default_timeout(timeout)
                    page.route(CDN + "**", lambda route: route.abort())  # live mode must not need the CDN
                    _watch(page, errors, port)
                    page.goto(f"http://127.0.0.1:{port}/console/?mode=live&notour")
                    page.wait_for_function(READY)
                    s = Smoke(page, shots, label)
                    results.append(s)
                    if s.check(
                        "console server answers and the console starts",
                        page.evaluate("document.body.dataset.ready") == "1",
                        s.text("main"),
                    ):
                        if label == "live desktop":
                            live_desktop(s)
                        else:
                            mobile_pass(s, "live-")
                    ctx.close()
            finally:
                server.should_exit = True
        browser.close()
    for e in errors:
        print("ERROR " + e)
    passed = sum(len(s.passed) for s in results)
    failed = sum(len(s.failed) for s in results)
    print(f"\n{passed} checks passed, {failed} failed, {len(errors)} page errors")
    if shots:
        print(f"screenshots in {shots}")
    return 0 if not failed and not errors else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--pyodide-dir", help="serve Pyodide 0.26.4 from this local directory instead of the CDN")
    ap.add_argument("--port", type=int, default=0, help="port for the local http.server (default: any free port)")
    ap.add_argument("--timeout", type=int, default=90, help="seconds to wait for any one step (default 90)")
    ap.add_argument("--screenshots", help="save desktop and mobile screenshots of every screen here")
    ap.add_argument("--live", action="store_true", help="also run the live-mode checks against src/console_server.py")
    ap.add_argument("--live-only", action="store_true", help="only the live-mode checks (no Pyodide)")
    ap.add_argument("--require", action="store_true", help="fail instead of skipping when Playwright is missing")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
