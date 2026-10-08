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
- The agent answers every call first. Calls it can't finish go to member services; containment
  improves over the period as intents are tuned (a typical post-launch curve).
- Fraud pressure is a steady background rate plus one SIM-swap campaign in late August.
- Cost avoided uses stated per-call cost assumptions, which the Business view shows next to it.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "data" / "sample_company.json"
SEED = 20261007
END = date(2026, 10, 7)
DAYS = 90

COMPANY = {
    "name": "Cypress Harbor Credit Union",
    "short": "Cypress Harbor CU",
    "fictional": True,
    "industry": "Credit union (financial services)",
    "headquarters": "Fort Lauderdale, Florida",
    "members": 92400,
    "assets_usd": 1_400_000_000,
    "employees": 340,
    "branches": 11,
    "contact_center": "Member services, 38 agents, 8am-7pm ET Mon-Sat; the AI agent answers 24/7",
    "regulators": ["NCUA", "CFPB", "Florida OFR"],
}

# Intent mix: share of calls, the agent's containment once tuned, and the tool it ends in.
INTENTS = [
    ("Balance and recent transactions", 0.21, 0.91, None),
    ("Loan payment", 0.16, 0.78, "take_payment"),
    ("Lost or stolen card", 0.11, 0.62, "create_ticket"),
    ("Dispute a card charge", 0.09, 0.55, "create_ticket"),
    ("Branch hours and locations", 0.08, 0.97, None),
    ("Appointment with a loan officer", 0.07, 0.83, "book_meeting"),
    ("Auto or home loan rates", 0.07, 0.69, "log_lead"),
    ("Update address, phone or email", 0.06, 0.41, "update_contact"),
    ("Fraud alert confirmation", 0.05, 0.58, None),
    ("Online banking login help", 0.06, 0.52, "create_ticket"),
    ("Other", 0.04, 0.22, None),
]

TRANSFER_REASONS = [
    ("Caller asked for a person", 0.31),
    ("Step-up verification not completed", 0.17),
    ("Risk score above threshold", 0.09),
    ("Intent not supported yet", 0.24),
    ("Payment over the per-call limit", 0.06),
    ("Complaint or hardship request", 0.13),
]

BRANCHES = [
    "Fort Lauderdale (HQ)",
    "Coral Springs",
    "Boca Raton",
    "Pompano Beach",
    "Plantation",
    "Hollywood",
    "Delray Beach",
    "Sunrise",
    "Weston",
    "Deerfield Beach",
    "Miramar",
]

ASSUMPTIONS = {
    "agent_cost_per_call_usd": 5.40,
    "ai_cost_per_call_usd": 0.46,
    "note": "Fully loaded member-services cost per handled call and AI cost per call (telephony, speech, "
    "model, compute). Illustrative assumptions for the sample company, not measurements.",
}


def _day(rng: random.Random, d: date, i: int) -> dict:
    weekday = d.weekday()
    base = 1020 * [1.22, 1.05, 1.0, 0.98, 1.03, 0.52, 0.31][weekday]
    if d.day in (1, 2, 15, 16):
        base *= 1.18  # paydays and loan due dates
    if d == date(2026, 9, 7):
        base *= 0.45  # Labor Day: branches closed, the agent still answers
    calls = int(base * rng.uniform(0.93, 1.07))
    progress = i / (DAYS - 1)
    containment = 0.58 + 0.10 * (1 - math.exp(-3.2 * progress)) + rng.uniform(-0.012, 0.012)
    abandoned = int(calls * rng.uniform(0.012, 0.022))
    contained = int((calls - abandoned) * containment)
    transferred = calls - abandoned - contained
    after_hours = int(calls * (0.19 if weekday < 5 else 0.34) * rng.uniform(0.92, 1.08))
    campaign = date(2026, 8, 24) <= d <= date(2026, 8, 30)
    sim_swap = rng.randint(0, 2) + (rng.randint(6, 11) if campaign else 0)
    se_handoffs = rng.randint(2, 6) + (rng.randint(3, 7) if campaign else 0)
    takeover = rng.randint(0, 2) + (rng.randint(2, 5) if campaign else 0)
    otp_lockouts = rng.randint(1, 4) + (rng.randint(2, 6) if campaign else 0)
    payments = int(calls * 0.16 * 0.78 * rng.uniform(0.9, 1.1))
    payment_usd = round(payments * rng.uniform(318, 372), 2)
    stepups = int(payments * 1.12 + calls * 0.05)
    stepup_passed = int(stepups * rng.uniform(0.9, 0.94))
    return {
        "date": d.isoformat(),
        "calls": calls,
        "contained": contained,
        "transferred": transferred,
        "abandoned": abandoned,
        "after_hours": after_hours,
        "avg_handle_seconds": int(rng.uniform(148, 176) - 18 * progress),
        "avg_answer_seconds": round(rng.uniform(0.8, 1.4), 1),
        "payments": payments,
        "payment_usd": payment_usd,
        "stepups": stepups,
        "stepup_passed": stepup_passed,
        "pii_scrubbed": int(calls * rng.uniform(0.028, 0.036)),
        "fraud_blocked": sim_swap + se_handoffs + takeover + otp_lockouts,
        "sim_swap_holds": sim_swap,
        "social_engineering_handoffs": se_handoffs,
        "takeover_patterns": takeover,
        "otp_lockouts": otp_lockouts,
        "csat": round(rng.uniform(4.3, 4.6) + 0.1 * progress, 2),
    }


def build() -> dict:
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    days = [_day(rng, start + timedelta(days=i), i) for i in range(DAYS)]
    total = sum(d["calls"] for d in days[-30:])
    intents = []
    for name, share, cont, tool in INTENTS:
        n = int(total * share * rng.uniform(0.95, 1.05))
        intents.append(
            {"intent": name, "calls_30d": n, "containment": round(cont + rng.uniform(-0.02, 0.02), 3), "tool": tool}
        )
    transfers_30d = sum(d["transferred"] for d in days[-30:])
    reasons = [{"reason": r, "calls_30d": int(transfers_30d * s)} for r, s in TRANSFER_REASONS]
    branches = []
    for i, b in enumerate(BRANCHES):
        weight = 1.6 if i == 0 else rng.uniform(0.7, 1.15)
        branches.append({"branch": b, "weight": weight})
    wsum = sum(b["weight"] for b in branches)
    appts = sum(int(d["calls"] * 0.07 * 0.83) for d in days[-30:])
    branch_rows = [
        {"branch": b["branch"], "appointments_30d": int(appts * b["weight"] / wsum)}
        for b in sorted(branches, key=lambda x: -x["weight"])
    ]
    notable = [
        {
            "date": "2026-10-06",
            "kind": "fraud",
            "title": "Account takeover pattern stopped",
            "detail": "Caller changed the email on file, then asked for a $4,800 payoff link on the same call. "
            "Payment handed to member services; member confirmed it wasn't them.",
        },
        {
            "date": "2026-10-03",
            "kind": "ops",
            "title": "Containment passed 67% for the first time",
            "detail": "Loan-payment and branch-hours intents tuned in the September policy review.",
        },
        {
            "date": "2026-09-30",
            "kind": "compliance",
            "title": "Quarterly audit-log verification: 100% intact",
            "detail": "Every call's hash chain verified for the quarter; evidence exported for the NCUA exam file.",
        },
        {
            "date": "2026-08-27",
            "kind": "fraud",
            "title": "SIM-swap campaign: 41 contact changes held",
            "detail": "Carrier risk signals flagged a wave of ported numbers. Step-up refused codes to swapped "
            "phones and handed callers to a person; no account changes went through.",
        },
        {
            "date": "2026-08-12",
            "kind": "ops",
            "title": "Keypad payments enabled for card payments",
            "detail": "PCI scope reduced: card numbers are keyed into Twilio Pay and never reach the agent or "
            "the recording.",
        },
    ]
    return {
        "generated_by": "scripts/generate_sample_company.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated data for demonstration, not measurements.",
        "period": {"start": start.isoformat(), "end": END.isoformat(), "days": DAYS},
        "company": COMPANY,
        "assumptions": ASSUMPTIONS,
        "days": days,
        "intents": sorted(intents, key=lambda x: -x["calls_30d"]),
        "transfer_reasons": sorted(reasons, key=lambda x: -x["calls_30d"]),
        "branch_appointments": branch_rows,
        "compliance": {
            "ai_disclosure_rate": 1.0,
            "recording_consent_asked_rate": 0.62,
            "recording_declined_rate": 0.07,
            "audit_chains_verified_rate": 1.0,
            "card_numbers_spoken_to_agent": 0,
        },
        "notable": notable,
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/generate_sample_company.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.write_text(text)
    d = json.loads(text)["days"][-30:]
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(x['calls'] for x in d):,} calls in the last 30 days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
