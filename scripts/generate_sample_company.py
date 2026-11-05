# Corey Mathie, 2026
"""
Write demo/data/sample_company.json: 90 days of contact-center activity for a fictional
mid-size credit union, so the console's Business view shows the agent at a realistic scale.

Cypress Harbor Credit Union does not exist. Every number in the file is generated here from a
fixed seed and the assumptions below; none of it is a measurement of this repo, a customer, or
any real institution. The console labels it "Sample company data" wherever it appears, and keeps
it apart from the measured eval results and the calls simulated in the browser.

    python scripts/generate_sample_company.py            # write the file
    python scripts/generate_sample_company.py --check    # exit 1 if the committed file differs

The model, in short:
- Inbound volume scales with membership (about 0.27 calls per member per month), with a Monday
  peak, light weekends, and payday spikes on the 1st and 15th.
- The last day is generated call by call (scripts/sample_calls.py): every one of its calls has a
  member, a transcript and the safeguards it went through, and that day's dashboard row is counted
  from those calls. The newest 320 are the console's call list.
- The other days scale from the last day's measured rates (step-ups per call for each intent,
  payments, scrubbed identifiers, consent declines, lockouts, call length) and the fraud rates the
  call generator uses. Containment improves over the period as intents are tuned and ends at the
  last day's measured value; satisfaction and call length follow the same curve.
- Day-to-day volume drifts (a slow random walk) on top of the weekly shape, with three events: a
  card-processor outage on September 18 (declined cards flood the line and more calls transfer),
  a tropical-storm watch on September 29-30, and Labor Day (member services closed).
- Member services is open 8am-7pm Monday to Saturday; the AI agent answers around the clock, so
  every Sunday call is after hours.
- Fraud pressure is the call generator's background rate plus one SIM-swap campaign in late August.
- Cost avoided uses per-call costs set from published figures (Gartner's cost per assisted contact; OpenAI
  and Twilio list prices for the AI call), which the Business view shows next to it. BENCHMARKS below
  lists every headline figure with its source, or labels it an assumption.
- The recent-activity entries are written from the data: every number in them is computed here.

It also writes demo/data/sample_calls.json: the 320 most recent calls on the last day.
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # sample_calls scores each call with src/safeguards/policy_gate.py

try:
    from scripts import sample_calls
except ImportError:  # run as python scripts/generate_sample_company.py
    import sample_calls

OUT = ROOT / "demo" / "data" / "sample_company.json"
CALLS_OUT = ROOT / "demo" / "data" / "sample_calls.json"
SEED = 20261007
END = date(2026, 10, 7)
DAYS = 90
RECENT_CALLS = 320
OUTAGE = date(2026, 9, 18)
STORM = (date(2026, 9, 29), date(2026, 9, 30))
LABOR_DAY = date(2026, 9, 7)
CAMPAIGN = (date(2026, 8, 24), date(2026, 8, 30))  # SIM-swap campaign, inclusive
WEEKDAY_SHAPE = [1.22, 1.05, 1.0, 0.98, 1.03, 0.52, 0.31]
TUNING_GAIN = 0.10  # containment gained over the 90 days as intents were tuned

COMPANY = {
    "name": "Cypress Harbor Credit Union",
    "short": "Cypress Harbor CU",
    "fictional": True,
    "industry": "Credit union (financial services)",
    "headquarters": "Fort Lauderdale, Florida",
    "members": 92400,
    "assets_usd": 1_600_000_000,
    "employees": 340,
    "branches": len(sample_calls.BRANCHES),
    "contact_center": "Member services, 38 agents, 8am-7pm ET Mon-Sat (closed Sunday); the AI agent answers 24/7",
    "regulators": ["NCUA", "CFPB", "Florida OFR"],
}

# Per-minute list prices behind the AI cost per call (see the "AI cost per call" row in BENCHMARKS): OpenAI
# Realtime audio at its launch rates, billed here for the whole call as both input and output (an upper
# bound), plus Twilio's charge to receive a call on a toll-free number and to stream its audio to the agent.
AI_PRICE_PER_MINUTE_USD = {
    "openai_audio_input": 0.06,
    "openai_audio_output": 0.24,
    "twilio_toll_free_inbound": 0.0220,
    "twilio_media_streams": 0.0044,
}

ASSUMPTIONS = {
    "agent_cost_per_call_usd": 8.01,
    "ai_cost_per_call_usd": 0.44,
    "note": "Member-services cost per agent-handled call: Gartner's 2019 average cost per live-channel contact "
    "($8.01), the lower of Gartner's two published figures. AI cost per call: OpenAI Realtime audio and Twilio "
    "toll-free voice and Media Streams list prices times the average call length; compute, storage and logging "
    "are not included. Set from published figures for the sample company, not measurements.",
}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---------- how the sample numbers were set ----------
#
# One row per headline figure on the Sample business view: the value in this sample (the 30 days to the
# latest sample day), what sets it, and the published benchmark it is calibrated to. A row may list
# `sources` only if each page was fetched and read and `quote` is the text the figure comes from; each
# source then needs title, publisher, year, url and quote, and the sample value must sit inside `range`
# (tests/test_sample_company.py checks this). A row with no sources is labelled derived (computed from
# other rows) or an assumption. The console's "How these sample numbers were set" panel and the table in
# docs/console.md both come from this list (through demo/data/sample_company.json).

UNVERIFIED = "Assumption (no public benchmark cited)"
DERIVED = "Derived from the rows above"
ACCESSED = "2026-11-05"

_NCUA = {
    "title": "Quarterly Credit Union Data Summary 2026 Q2",
    "publisher": "National Credit Union Administration (NCUA)",
    "year": 2026,
    "url": "https://ncua.gov/files/publications/analysis/quarterly-data-summary-2026-Q2.pdf",
}
_GARTNER_2019 = {
    "title": "Gartner Says Only 9% of Customers Report Solving Their Issues Completely via Self-Service",
    "publisher": "Gartner (press release)",
    "year": 2019,
    "url": "https://www.gartner.com/en/newsroom/press-releases/2019-09-25-gartner-says-only-9--of-customers-report-solving-thei",
}
_TWILIO = {
    "title": "Programmable Voice Pricing in United States",
    "publisher": "Twilio (price list, current as of August 2026)",
    "year": 2026,
    "url": "https://www.twilio.com/en-us/voice/pricing/us",
}

# A row with `sources` is calibrated: the sample value sits in `range` (low, high) where one is given, and every
# source carries the page's exact words in `quote` (`where` names the table, for figures read from a table).
BENCHMARKS = [
    {"figure": "Calls answered by the agent", "key": "calls",
     "basis": "Volume model in generate_sample_company.py: about 1,020 calls on a typical weekday at the "
              "start of the period, "
              "a weekly shape, payday peaks and three events. No public figure for calls per credit union "
              "member was found."},
    {"figure": "Resolved without a transfer", "key": "containment",
     "basis": "Each intent's resolution rate in scripts/sample_calls.py, with a tuning gain of 10 points over "
              "the 90 days. Compared with phone self-service (IVR) resolution in SQM Group's benchmarking.",
     "benchmark": "63% one-contact resolution to 74% first-call resolution for IVR self-service",
     "range": (63.0, 74.0),
     "sources": [
         {"title": "IVR Dilemma - Customer Containment Vs. Customer Experience Focus?", "publisher": "SQM Group",
          "year": 2021, "url": "https://www.sqmgroup.com/resources/library/blog/ivr-dilemma",
          "quote": "Our research shows that the IVR self-service First Call Resolution (FCR) rate is 74%, which is "
                   "the highest FCR rate of all contact channels. One Contact Resolution rate is 63% (OCR measures "
                   "if the call was resolved in one call and one contact channel)."},
     ]},
    {"figure": "Average call length", "key": "avg_handle",
     "basis": "Scripted call timings in scripts/sample_calls.py: the disclosure, the consent prompt and each turn."},
    {"figure": "Member satisfaction", "key": "csat",
     "basis": "Post-call survey ratings drawn per call outcome in scripts/sample_calls.py. No public benchmark "
              "on a 1-5 scale was found."},
    {"figure": "Member-services cost per agent-handled call", "key": "agent_cost",
     "basis": "ASSUMPTIONS in generate_sample_company.py, set to Gartner's 2019 average cost per live-channel "
              "contact, the lower of the two Gartner figures (was $5.40, below both).",
     "benchmark": "$8.01 (2019 average, live channels) to $13.50 (2024 median, assisted channels)",
     "range": (8.01, 13.50),
     "sources": [
         {**_GARTNER_2019,
          "quote": "Gartner\u2019s 2019 Customer Service and Support Leader poll identified that live channels such "
                   "as phone, live chat and email cost an average of $8.01 per contact, while self-service "
                   "channels such as company-run websites and mobile apps cost about $0.10 per contact."},
         {"title": "Benchmarks to Assess Your Customer Service Costs (public abstract)", "publisher": "Gartner",
          "year": 2024, "url": "https://www.gartner.com/en/documents/5164231",
          "quote": "The median cost per contact is $1.84 for self-service and $13.50 for assisted channels."},
     ]},
    {"figure": "AI cost per call", "key": "ai_cost",
     "basis": "AI_PRICE_PER_MINUTE_USD times the 30-day average call length: OpenAI Realtime audio at $0.06 a "
              "minute in and $0.24 a minute out, both billed for the whole call (an upper bound), plus Twilio "
              "toll-free inbound ($0.0220) and Media Streams ($0.0044). Compute, storage and logging are not "
              "included (was $0.46).",
     "benchmark": "$0.3264 a minute in list prices",
     "sources": [
         {"title": "Introducing the Realtime API", "publisher": "OpenAI", "year": 2024,
          "url": "https://openai.com/index/introducing-the-realtime-api/",
          "quote": "Audio input is priced at $100 per 1M tokens and output is $200 per 1M tokens. This equates to "
                   "approximately $0.06 per minute of audio input and $0.24 per minute of audio output."},
         {**_TWILIO, "where": "Receive calls table", "quote": "Toll-free numbers $0.0220/ min + $2.15 / mo"},
         {**_TWILIO, "where": "Intelligent service pricing table",
          "quote": "Media Streams Send a copy of a call's audio to a partner stream connector or to a secure "
                   "websocket for real-processing. $0.0044/ min"},
     ]},
    {"figure": "Member-services cost avoided", "key": "cost_avoided", "derived": True,
     "basis": "Calls resolved by the agent times the difference between the two per-call costs above."},
    {"figure": "Calls after hours", "key": "after_hours",
     "basis": "Hourly call shape in scripts/sample_calls.py against member-services hours (8am-7pm Mon-Sat)."},
    {"figure": "Fraud attempts stopped", "key": "fraud",
     "basis": "Per-call rates in scripts/sample_calls.py (social engineering 0.45%, SIM swap 0.35%, account "
              "takeover 0.3%) plus one SIM-swap campaign."},
    {"figure": "Assets per member", "key": "assets_per_member",
     "basis": "COMPANY in generate_sample_company.py: $1.6B in assets and 92,400 members (assets were $1.4B, "
              "$15,152 per member, below both NCUA figures). NCUA: $2,498.9B over 146.1M members for all "
              "federally insured credit unions; $2,186.3B over 124.2M members for those over $500M in assets.",
     "benchmark": "$17,104 (all federally insured credit unions) to $17,603 (assets over $500 million), June 30, 2026",
     "range": (17104, 17603),
     "sources": [
         {**_NCUA,
          "quote": "As of June 30, 2026, there were 4,214 federally insured credit unions with 146.1 million members."},
         {**_NCUA, "where": "Summary of Federally Insured Credit Union Call Report Data, Total assets, 2026.2",
          "quote": "Total assets $ Billions 2,498.9"},
         {**_NCUA, "where": "Summary of Credit Union Indicators by Type of Credit Union, Complex Credit Unions "
                            "(Assets greater than $500 million)",
          "quote": "Number of members (millions) 124.2; Total assets ($ billions) 2,186.3"},
     ]},
    {"figure": "Members per branch", "key": "members_per_branch",
     "basis": "COMPANY in generate_sample_company.py: 92,400 members and 11 branches. Inside the middle half for "
              "credit unions with $500 million or more in assets, as computed from NCUA call reports by a "
              "third-party site (not NCUA).",
     "benchmark": "4,870 to 9,430 (middle half, median 6,584) for credit unions with $500 million or more in "
                  "assets, Q2 2026",
     "range": (4870, 9430),
     "sources": [
         {"title": "Credit union members per branch", "publisher": "Credit Union Ledger (creditunions.online), from "
          "NCUA 5300 Call Report data; not affiliated with NCUA", "year": 2026,
          "url": "https://creditunions.online/metrics/members-per-branch/",
          "where": "By NCUA peer group table (Assets, Count, 25th pct, Median, 75th pct)",
          "quote": "$500 million and up 758 4,870 6,584 9,430"},
     ]},
]  # fmt: skip


def _volume(rng: random.Random, d: date, drift: float) -> int:
    base = 1020 * drift * WEEKDAY_SHAPE[d.weekday()]
    if d.day in (1, 2, 15, 16):
        base *= 1.18  # paydays and loan due dates
    if d == LABOR_DAY:
        base *= 0.45  # branches and member services closed; the agent still answers
    if d == OUTAGE:
        base *= 1.45  # card-processor outage: members call about declined cards
    if d in STORM:
        base *= 1.3 if d == STORM[0] else 1.12  # storm watch: branch closures, payment deferrals
    return int(base * rng.uniform(0.9, 1.1))


def _binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(rng.random() < p for _ in range(n))


def _tuning(progress: float) -> float:
    """How far below its final containment a day sits: TUNING_GAIN at the start, 0 on the last day."""
    k = 3.2
    return TUNING_GAIN * (math.exp(-k * progress) - math.exp(-k)) / (1 - math.exp(-k))


def _day(rng: random.Random, d: date, i: int, calls: int, rates: dict) -> dict:
    """A day scaled from the last day's measured rates (see sample_calls.measure)."""
    end = rates["row"]
    progress = i / (DAYS - 1)
    containment = rates["containment"] - _tuning(progress) + rng.uniform(-0.02, 0.02)
    if d == OUTAGE:
        containment -= 0.11
    abandoned = round(calls * rates["abandon"] * rng.uniform(0.75, 1.25))
    contained = round(calls * containment)
    transferred = calls - abandoned - contained
    closed = d.weekday() == 6 or d == LABOR_DAY
    after_hours = (
        calls
        if closed
        else min(calls, round(calls * sample_calls.after_hours_share(d.weekday()) * rng.uniform(0.92, 1.08)))
    )
    by_intent = {name: calls * share for name, share, _tool in sample_calls.INTENT_MIX}
    stepups = round(sample_calls.stepups_for(by_intent, rates) * rng.uniform(0.95, 1.05))
    payments = round(calls * rates["payments_per_call"] * rng.uniform(0.9, 1.1))
    pii = round(calls * rates["pii_per_call"] * rng.uniform(0.8, 1.2))
    campaign = CAMPAIGN[0] <= d <= CAMPAIGN[1]
    f = sample_calls.FRAUD_RATES
    sim_swap = _binomial(rng, calls, f["simswap"]) + (rng.randint(6, 11) if campaign else 0)
    social = _binomial(rng, calls, f["social"]) + (rng.randint(3, 7) if campaign else 0)
    takeover = _binomial(rng, calls, f["takeover"]) + (rng.randint(2, 5) if campaign else 0)
    return {
        "date": d.isoformat(),
        "calls": calls,
        "contained": contained,
        "transferred": transferred,
        "abandoned": abandoned,
        "after_hours": after_hours,
        "avg_handle_seconds": round(end["avg_handle_seconds"] + 18 * (1 - progress) + rng.uniform(-6, 6)),
        "avg_answer_seconds": round(rng.uniform(0.8, 1.4), 1),
        "payments": payments,
        "payment_usd": round(payments * rates["usd_per_payment"] * rng.uniform(0.93, 1.07), 2),
        "stepups": stepups,
        "stepup_passed": round(stepups * min(0.99, rates["stepup_pass"] + rng.uniform(-0.015, 0.015))),
        "pii_scrubbed": pii,
        "card_numbers_scrubbed": min(pii, round(pii * rates["card_share_of_pii"] * rng.uniform(0.9, 1.1))),
        "recording_declined": round(calls * rates["declined_per_call"] * rng.uniform(0.8, 1.2)),
        "fraud_blocked": sim_swap + social + takeover,
        "sim_swap_holds": sim_swap,
        "social_engineering_handoffs": social,
        "takeover_patterns": takeover,
        "otp_lockouts": _binomial(rng, calls, rates["lockouts_per_call"]),
        "csat": round(min(4.9, end["csat"] - 0.12 * (1 - progress) + rng.uniform(-0.06, 0.06)), 2),
    }


def _last_day_row(rng: random.Random, full: list[dict]) -> dict:
    row = sample_calls.day_row(full)
    keys = [
        "calls", "contained", "transferred", "abandoned", "after_hours", "avg_handle_seconds", "payments",
        "payment_usd", "stepups", "stepup_passed", "pii_scrubbed", "card_numbers_scrubbed", "recording_declined",
        "fraud_blocked", "sim_swap_holds", "social_engineering_handoffs", "takeover_patterns", "otp_lockouts", "csat",
    ]  # fmt: skip
    out = {"date": END.isoformat(), **{k: row[k] for k in keys}}
    out["avg_answer_seconds"] = round(rng.uniform(0.8, 1.4), 1)
    return out


def _money_short(n: float) -> str:
    """The console's dashboard format (demo/format.js moneyShort): $1.6B, $1.46M, $142K."""
    if n >= 1e9:
        return f"${n / 1e9:.1f}B".replace(".0B", "B")
    if n >= 1e6:
        digits = 1 if n >= 1e7 else 2
        return "$" + f"{n / 1e6:.{digits}f}".rstrip("0").rstrip(".") + "M"
    if n >= 1e4:
        return f"${round(n / 1e3)}K"
    return f"${round(n):,}"


def ai_cost_per_call(days: list[dict]) -> float:
    """AI_PRICE_PER_MINUTE_USD times the average call length over the last 30 days, to the cent."""
    last = days[-30:]
    minutes = sum(d["avg_handle_seconds"] * d["calls"] for d in last) / sum(d["calls"] for d in last) / 60
    return round(minutes * sum(AI_PRICE_PER_MINUTE_USD.values()), 2)


def benchmark_values(days: list[dict]) -> dict[str, tuple[str, float]]:
    """Each headline figure over the last 30 days: (as shown, as a number comparable with the row's range)."""
    last = days[-30:]
    calls = sum(d["calls"] for d in last)
    contained = sum(d["contained"] for d in last)
    handle = sum(d["avg_handle_seconds"] * d["calls"] for d in last) / calls
    csat = sum(d["csat"] * d["calls"] for d in last) / calls
    after = sum(d["after_hours"] for d in last) / calls * 100
    fraud = sum(d["fraud_blocked"] for d in last)
    a = ASSUMPTIONS
    avoided = contained * (a["agent_cost_per_call_usd"] - a["ai_cost_per_call_usd"])
    per_member = COMPANY["assets_usd"] / COMPANY["members"]
    per_branch = COMPANY["members"] / COMPANY["branches"]
    return {
        "calls": (f"{calls:,} in 30 days ({calls / COMPANY['members']:.2f} per member)", calls),
        "containment": (f"{contained / calls * 100:.1f}%", contained / calls * 100),
        "avg_handle": (f"{int(handle // 60)}m {round(handle % 60):02d}s", handle),
        "csat": (f"{csat:.2f} / 5", csat),
        "agent_cost": (f"${a['agent_cost_per_call_usd']:.2f}", a["agent_cost_per_call_usd"]),
        "ai_cost": (f"${a['ai_cost_per_call_usd']:.2f}", a["ai_cost_per_call_usd"]),
        "cost_avoided": (_money_short(avoided), avoided),
        "after_hours": (f"{after:.1f}% of calls", after),
        "fraud": (f"{fraud:,} in 30 days", fraud),
        "assets_per_member": (f"${per_member:,.0f}", per_member),
        "members_per_branch": (f"{per_branch:,.0f}", per_branch),
    }


def benchmark_rows(days: list[dict]) -> list[dict]:
    """BENCHMARKS with each figure's value over the last 30 days, and its sources or its assumption label."""
    values = benchmark_values(days)
    rows = []
    for b in BENCHMARKS:
        sources = [{**s, "accessed": ACCESSED} for s in b.get("sources", [])]
        status = "cited" if sources else "derived" if b.get("derived") else "assumption"
        label = {"cited": "Calibrated to a published benchmark", "derived": DERIVED, "assumption": UNVERIFIED}
        rows.append(
            {
                "figure": b["figure"],
                "value": values[b["key"]][0],
                "basis": b["basis"],
                "status": status,
                "label": label[status],
                "benchmark": b.get("benchmark"),
                "sources": sources,
            }
        )
    return rows


def benchmarks_markdown(rows: list[dict]) -> str:
    """The table and quoted sources in docs/console.md, between its benchmarks:start and benchmarks:end markers."""
    lines = [
        "| Figure | In this sample (30 days) | What sets it | Published benchmark | Source |",
        "|---|---|---|---|---|",
    ]
    quotes = []
    for r in rows:
        refs = {s["url"]: f"[{s['title']}]({s['url']}), {s['publisher']}, {s['year']}" for s in r["sources"]}
        for s in r["sources"]:
            where = f" ({s['where']})" if s.get("where") else ""
            quotes.append(f'- **{r["figure"]}**, {s["title"]}{where}: "{s["quote"]}"')
        cell = "; ".join(refs.values()) or r["label"]
        lines.append(f"| {r['figure']} | {r['value']} | {r['basis']} | {r['benchmark'] or '-'} | {cell} |")
    lines += ["", f"Quoted text, exactly as on each page (all read on {ACCESSED}):", "", *quotes]
    return "\n".join(lines)


# ---------- recent activity, written from the data ----------


def rolling_containment(days: list[dict], window: int = 7) -> list[tuple[str, float]]:
    out = []
    for i in range(window - 1, len(days)):
        w = days[i - window + 1 : i + 1]
        out.append((w[-1]["date"], sum(d["contained"] for d in w) / sum(d["calls"] for d in w)))
    return out


def vs_same_weekday(days: list[dict], day: date, field: str = "calls", weeks: int = 4) -> float:
    """A day's value against the mean of the same weekday over the previous `weeks` weeks."""
    by_date = {d["date"]: d for d in days}
    prior = [by_date[(day - timedelta(weeks=k)).isoformat()][field] for k in range(1, weeks + 1)]
    return by_date[day.isoformat()][field] / (sum(prior) / len(prior)) - 1


def _transfer_share(d: dict) -> float:
    return d["transferred"] / d["calls"]


def _notable(days: list[dict], shown: list[dict]) -> list[dict]:
    by_date = {d["date"]: d for d in days}
    notes = []

    spanish = [c for c in shown if c["language"] == "es"]
    to_queue = sum(c["outcome"] == "transferred" for c in spanish)
    notes.append(
        {
            "date": END.isoformat(),
            "kind": "ops",
            "title": "Spanish-language calls handled end to end in Spanish",
            "detail": f"{len(spanish)} of the latest {len(shown)} calls were in Spanish: verified, served and "
            f"summarized in Spanish, with {to_queue} transferred to the bilingual queue.",
        }
    )

    fraud = [c for c in shown if "Fraud stopped" in c["flags"]]
    order = {"Account-takeover pattern": 0, "Social-engineering score": 1, "SIM-swap signal": 2}

    def kind(c: dict) -> int:
        return min(order.get(g["control"], 9) for g in c["safeguards"])

    pick = min(fraud, key=kind)
    if kind(pick) == 0:
        amount = next(t["text"] for t in pick["transcript"] if t["who"] == "member" and "payoff link" in t["text"])
        usd = amount.split("for ", 1)[1].split(" ", 1)[0]
        title, detail = (
            "Account takeover pattern stopped",
            f"A caller changed the email on file, then asked for a {usd} payoff link on the same call. The payment "
            "went to member services, who contact the member using the details on file before the change.",
        )
    elif kind(pick) == 1:
        title, detail = (
            "Social-engineering attempt stopped",
            f"A caller claiming to own a business account pressed for a payment link to a new number. Risk score "
            f"{pick['risk_score']} handed the payment to a person; no link was sent.",
        )
    else:
        title, detail = (
            "SIM-swap hold",
            "A caller asked to change the email on file from a phone the carrier had just reported as SIM-swapped. "
            "No code was sent and nothing changed.",
        )
    notes.append({"date": END.isoformat(), "kind": "fraud", "title": title, "detail": detail, "call": pick["id"]})

    last30 = days[-30:]
    cards = sum(d["card_numbers_scrubbed"] for d in last30)
    notes.append(
        {
            "date": END.isoformat(),
            "kind": "compliance",
            "title": f"Card numbers read aloud on {cards:,} calls in 30 days, all scrubbed",
            "detail": "Members sometimes read a full card number during a dispute. Each one was removed from the "
            "transcript, the case and the CRM note before anything was saved; card payments go through the keypad, "
            "so the agent never needs one.",
        }
    )

    rolling = rolling_containment(days)
    threshold = math.floor(rolling[-1][1] * 100)
    first = next(day for day, v in rolling if v * 100 >= threshold)
    notes.append(
        {
            "date": first,
            "kind": "ops",
            "title": f"Seven-day containment passed {threshold}% for the first time",
            "detail": "The share of calls resolved without a transfer over the last seven days, after loan-payment "
            "and branch-hours intents were tuned in the policy reviews.",
        }
    )

    storm = STORM[0]
    up = round(vs_same_weekday(days, storm) * 100)
    weekday = WEEKDAYS[storm.weekday()]
    notes.append(
        {
            "date": storm.isoformat(),
            "kind": "ops",
            "title": f"Tropical storm watch: {up}% more calls than a usual {weekday}",
            "detail": f"Compared with the previous four {weekday}s. Branch-closure and payment-deferral questions "
            "spiked; the agent answered every call, and hardship requests went to member services with the "
            "context attached.",
        }
    )

    up = round(vs_same_weekday(days, OUTAGE) * 100)
    weekday = WEEKDAYS[OUTAGE.weekday()]
    prior = [by_date[(OUTAGE - timedelta(weeks=k)).isoformat()] for k in range(1, 5)]
    usual = round(sum(_transfer_share(d) for d in prior) / len(prior) * 100)
    that_day = round(_transfer_share(by_date[OUTAGE.isoformat()]) * 100)
    notes.append(
        {
            "date": OUTAGE.isoformat(),
            "kind": "ops",
            "title": "Card processor outage: declined-card calls absorbed",
            "detail": f"A 3-hour outage at the card processor put calls {up}% above the previous four {weekday}s. "
            f"The agent explained the outage; {that_day}% of calls went to a person that day, against {usual}% on "
            f"those {weekday}s.",
        }
    )

    notes.append(
        {
            "date": "2026-09-30",
            "kind": "compliance",
            "title": "Quarterly audit-log verification: 100% intact",
            "detail": "Every call's hash chain verified for the quarter; evidence exported for the NCUA exam file.",
        }
    )

    held = sum(by_date[(CAMPAIGN[0] + timedelta(days=k)).isoformat()]["sim_swap_holds"] for k in range(7))
    notes.append(
        {
            "date": CAMPAIGN[1].isoformat(),
            "kind": "fraud",
            "title": f"SIM-swap campaign: {held} callers held in 7 days",
            "detail": "Carrier risk signals flagged a wave of recently swapped phones over the week to this date. "
            "Step-up refused to send codes to them and handed the callers to a person; no account changes went "
            "through.",
        }
    )

    notes.append(
        {
            "date": "2026-08-12",
            "kind": "ops",
            "title": "Keypad payments enabled for card payments",
            "detail": "PCI scope reduced: card numbers are keyed into Twilio Pay and never reach the agent or "
            "the recording.",
        }
    )
    return sorted(notes, key=lambda n: n["date"], reverse=True)


# ---------- the files ----------


@functools.lru_cache(maxsize=1)
def _generate() -> tuple[dict, dict]:
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    dates = [start + timedelta(days=i) for i in range(DAYS)]
    volumes, drift = [], 1.0
    for d in dates:
        drift = min(1.12, max(0.9, drift + rng.gauss(0, 0.018)))
        volumes.append(_volume(rng, d, drift))

    full = sample_calls.build_day(random.Random(SEED + 1), END, volumes[-1], RECENT_CALLS)
    rates = sample_calls.measure(full)
    days = [_day(rng, d, i, volumes[i], rates) for i, d in enumerate(dates[:-1])]
    days.append(_last_day_row(rng, full))
    shown = sample_calls.published(full)

    total = sum(d["calls"] for d in days[-30:])
    intents = []
    for name, share, tool in sample_calls.INTENT_MIX:
        intents.append(
            {
                "intent": name,
                "calls_30d": int(total * share * rng.uniform(0.95, 1.05)),
                "containment": round(rates["intents"][name]["containment"], 3),
                "tool": tool,
            }
        )
    transfers_30d = sum(d["transferred"] for d in days[-30:])
    reasons = [{"reason": r, "calls_30d": round(transfers_30d * s)} for r, s in rates["transfer_reasons"].items()]
    weights = {b: (1.6 if i == 0 else rng.uniform(0.7, 1.15)) for i, b in enumerate(sample_calls.BRANCHES)}
    wsum = sum(weights.values())
    appt = rates["intents"]["Appointment with a loan officer"]["containment"]
    share = next(s for n, s, _t in sample_calls.INTENT_MIX if n == "Appointment with a loan officer")
    appts = sum(round(d["calls"] * share * appt) for d in days[-30:])
    branch_rows = [
        {"branch": b, "appointments_30d": int(appts * w / wsum)}
        for b, w in sorted(weights.items(), key=lambda x: -x[1])
    ]
    company = {
        "generated_by": "scripts/generate_sample_company.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated data for demonstration, not measurements.",
        "period": {"start": start.isoformat(), "end": END.isoformat(), "days": DAYS},
        "company": COMPANY,
        "assumptions": ASSUMPTIONS,
        "benchmarks": benchmark_rows(days),
        "days": days,
        "intents": sorted(intents, key=lambda x: -x["calls_30d"]),
        "transfer_reasons": sorted(reasons, key=lambda x: (-x["calls_30d"], x["reason"])),
        "branch_appointments": branch_rows,
        "compliance": {
            "ai_disclosure_rate": 1.0,
            "recording_consent_state": "Florida",
            "recording_consent_asked_rate": 1.0,
            "audit_chains_verified_rate": 1.0,
        },
        "notable": _notable(days, shown),
        "recent_calls": {"file": "sample_calls.json", "count": len(shown), "through": shown[0]["started"]},
    }
    calls = {
        "generated_by": "scripts/generate_sample_company.py (scripts/sample_calls.py)",
        "seed": SEED + 1,
        "disclaimer": "Fictional members and calls. Generated for demonstration; no real person or account.",
        "day": END.isoformat(),
        "day_calls": len(full),
        "calls": shown,
    }
    return company, calls


def build() -> dict:
    return copy.deepcopy(_generate()[0])


def build_calls() -> dict:
    return copy.deepcopy(_generate()[1])


def build_day_calls() -> list[dict]:
    """Every call on the last day, with the generator's bookkeeping fields (for tests)."""
    return sample_calls.build_day(random.Random(SEED + 1), END, build()["days"][-1]["calls"], RECENT_CALLS)


def render_calls(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n"


def render(data: dict) -> str:
    return json.dumps(data, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    outputs = [(OUT, render(build())), (CALLS_OUT, render_calls(build_calls()))]
    if args.check:
        stale = [p for p, text in outputs if not p.exists() or p.read_text(encoding="utf-8") != text]
        for p in stale:
            print(f"{p.relative_to(ROOT)} is stale: run python scripts/generate_sample_company.py")
        if not stale:
            print("demo/data/sample_company.json and sample_calls.json are current")
        return 1 if stale else 0
    for p, text in outputs:
        p.write_text(text, encoding="utf-8")
    d = build()["days"][-30:]
    reasons = Counter(c["transfer_reason"] for c in build_calls()["calls"] if c["transfer_reason"])
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(x['calls'] for x in d):,} calls in the last 30 days")
    print(f"wrote {CALLS_OUT.relative_to(ROOT)}: {len(build_calls()['calls'])} calls, {len(reasons)} transfer reasons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
