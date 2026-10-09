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
- Cost avoided uses stated per-call cost assumptions, which the Business view shows next to it.
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
    "assets_usd": 1_400_000_000,
    "employees": 340,
    "branches": len(sample_calls.BRANCHES),
    "contact_center": "Member services, 38 agents, 8am-7pm ET Mon-Sat (closed Sunday); the AI agent answers 24/7",
    "regulators": ["NCUA", "CFPB", "Florida OFR"],
}

ASSUMPTIONS = {
    "agent_cost_per_call_usd": 5.40,
    "ai_cost_per_call_usd": 0.46,
    "note": "Fully loaded member-services cost per handled call and AI cost per call (telephony, speech, "
    "model, compute). Illustrative assumptions for the sample company, not measurements.",
}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


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
