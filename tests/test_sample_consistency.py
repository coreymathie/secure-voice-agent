# Corey Mathie, 2026
"""
The sample credit union tells one story: the last day's dashboard row is counted from that day's calls,
the call list is the newest of those calls, and the activity feed, the compliance panel and the KPI
definitions all agree with both. Each test here pins one place where they used to drift apart.
"""

import re
import shutil
import statistics
import subprocess
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from scripts import generate_sample_company as gen
from scripts import sample_calls as sc

ROOT = Path(__file__).resolve().parents[1]
FRAUD_CONTROLS = {"Social-engineering score", "SIM-swap signal", "Account-takeover pattern"}


@pytest.fixture(scope="module")
def company():
    return gen.build()


@pytest.fixture(scope="module")
def calls():
    return gen.build_calls()["calls"]


@pytest.fixture(scope="module")
def day():
    return gen.build_day_calls()


def _members(calls):
    return Counter((c["member"]["name"], c["member"]["member_no"]) for c in calls)


def _texts(c, who=None):
    return [t["text"] for t in c["transcript"] if who is None or t["who"] == who]


def _numbers(text):
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


# ---------- the day's KPI row, the call list and the day's calls ----------


def test_the_last_day_row_is_counted_from_its_calls(company, calls, day):
    row = company["days"][-1]
    counted = sc.day_row(day)
    assert {k: row[k] for k in counted} == counted
    assert row["calls"] == len(day) == gen.build_calls()["day_calls"]
    # The call list is the newest calls of that day, newest first, with nothing missing in between.
    shown = [c["id"] for c in day if c["published"]][::-1]
    assert [c["id"] for c in calls] == shown and len(calls) == gen.RECENT_CALLS
    oldest = calls[-1]["started"]
    assert all(c["published"] for c in day if c["started"] > oldest)


def test_the_call_list_never_shows_more_than_the_day(company, calls):
    row = company["days"][-1]
    flags = Counter(f for c in calls for f in c["flags"])
    out = Counter(c["outcome"] for c in calls)
    assert out["resolved"] <= row["contained"] and out["transferred"] <= row["transferred"]
    assert out["abandoned"] <= row["abandoned"]
    assert flags["Fraud stopped"] <= row["fraud_blocked"]
    assert flags["Verification lockout"] <= row["otp_lockouts"]
    assert flags["SIM-swap hold"] <= row["sim_swap_holds"]
    assert (
        sum(any(g["control"] == "Account-takeover pattern" for g in c["safeguards"]) for c in calls)
        <= row["takeover_patterns"]
    )
    assert sum(c["recording"] == "declined" for c in calls) <= row["recording_declined"]
    # Same calls, same length: the list's average is close to the day's (later calls skew short).
    assert abs(statistics.mean(c["duration_s"] for c in calls) - row["avg_handle_seconds"]) < 15


def test_step_ups_are_counted_per_intent(company, day):
    row = company["days"][-1]
    sent = [sum("one-time code sent" in g["result"] for g in c["safeguards"]) for c in day]
    assert sum(sent) == row["stepups"]
    rates = sc.measure(day)
    for name in ("Branch hours and locations", "Appointment with a loan officer", "Auto or home loan rates"):
        assert rates["intents"][name]["stepups_per_call"] == 0, name
    for name in ("Balance and recent transactions", "Loan or card payment", "Dispute a card charge"):
        assert rates["intents"][name]["stepups_per_call"] > 0.9, name
    # Every other day is each intent's calls times that intent's step-ups per call (within the day's noise).
    for d in company["days"][:-1]:
        expected = sc.stepups_for({n: d["calls"] * s for n, s, _t in sc.INTENT_MIX}, rates)
        assert abs(d["stepups"] - expected) <= 0.06 * expected, d["date"]
    last30 = company["days"][-30:]
    share = sum(d["stepups"] for d in last30) / sum(d["calls"] for d in last30)
    assert abs(share - row["stepups"] / row["calls"]) < 0.05


def test_every_payment_was_verified_first(calls):
    for c in calls:
        if any(a["tool"] == "take_payment" for a in c["actions"]):
            assert c["verification"] == "verified by SMS code", c["id"]


def test_fraud_attempts_and_lockouts_are_counted_apart(company, calls, day):
    for d in company["days"]:
        assert d["fraud_blocked"] == d["sim_swap_holds"] + d["social_engineering_handoffs"] + d["takeover_patterns"]
    for c in day:
        fraud = {g["control"] for g in c["safeguards"] if g["control"] in FRAUD_CONTROLS and g["kind"] == "block"}
        assert ("Fraud stopped" in c["flags"]) == bool(fraud), c["id"]
        if "Verification lockout" in c["flags"]:
            assert "Fraud stopped" not in c["flags"], c["id"]
    assert sum("Fraud stopped" in c["flags"] for c in day) == company["days"][-1]["fraud_blocked"]
    assert any("Fraud stopped" in c["flags"] for c in calls)
    app = (ROOT / "demo" / "app.js").read_text()
    assert "Code-guessing lockouts" not in app and "Not counted as fraud" in app


# ---------- recent activity: every number is computed from the data ----------


def _note(company, title_start):
    return next(n for n in company["notable"] if n["title"].startswith(title_start))


def _claims(note):
    return _numbers(note["title"]) | _numbers(note["detail"])


def test_activity_feed_spanish_calls(company, calls):
    n = _note(company, "Spanish-language calls")
    spanish = [c for c in calls if c["language"] == "es"]
    queued = [c for c in spanish if c["outcome"] == "transferred"]
    assert queued and all(c["queue"] == "Bilingual member services" for c in queued)
    assert _claims(n) == {str(len(spanish)), str(len(calls)), str(len(queued))}


def test_activity_feed_fraud_call_is_in_the_list(company, calls):
    n = next(x for x in company["notable"] if x["kind"] == "fraud" and x["date"] == gen.END.isoformat())
    call = next(c for c in calls if c["id"] == n["call"])
    assert "Fraud stopped" in call["flags"]
    if n["title"].startswith("Account takeover"):
        assert company["days"][-1]["takeover_patterns"] >= 1
        (amount,) = re.findall(r"\$[\d,]+\.\d\d", n["detail"])
        assert any(amount in t for t in _texts(call, "member"))
        assert sum(amount in " ".join(_texts(c)) for c in calls) == 1, "the amount names one call"
        assert _claims(n) == {amount[1:].replace(",", "")}


def test_activity_feed_card_numbers_and_compliance(company):
    n = _note(company, "Card numbers read aloud")
    cards = sum(d["card_numbers_scrubbed"] for d in company["days"][-30:])
    assert _claims(n) == {str(cards), "30"}
    assert "card_numbers_spoken_to_agent" not in company["compliance"]
    app = (ROOT / "demo" / "app.js").read_text()
    assert "Card numbers heard by the agent" not in app


def test_activity_feed_containment_first_time(company):
    n = _note(company, "Seven-day containment passed")
    (threshold,) = (int(x) for x in re.findall(r"(\d+)%", n["title"]))
    rolling = gen.rolling_containment(company["days"])
    first = next(d for d, v in rolling if v * 100 >= threshold)
    assert n["date"] == first
    assert all(v * 100 < threshold for d, v in rolling if d < first)
    assert threshold == int(rolling[-1][1] * 100)
    assert _claims(n) == {str(threshold)}


@pytest.mark.parametrize(("start", "when"), [("Tropical storm", gen.STORM[0]), ("Card processor", gen.OUTAGE)])
def test_activity_feed_events_against_the_same_weekday(company, start, when):
    n = _note(company, start)
    assert n["date"] == when.isoformat()
    up = round(gen.vs_same_weekday(company["days"], when) * 100)
    weekday = gen.WEEKDAYS[when.weekday()]
    assert f"previous four {weekday}s" in n["detail"]
    assert str(up) in _claims(n) and up > 0
    if start == "Card processor":
        by_date = {d["date"]: d for d in company["days"]}
        prior = [by_date[(when - timedelta(weeks=k)).isoformat()] for k in range(1, 5)]
        usual = round(statistics.mean(d["transferred"] / d["calls"] for d in prior) * 100)
        that_day = round(by_date[when.isoformat()]["transferred"] / by_date[when.isoformat()]["calls"] * 100)
        assert _claims(n) == {"3", str(up), str(that_day), str(usual)}  # 3: the outage's hours
    else:
        assert _claims(n) == {str(up)}


def test_activity_feed_sim_swap_campaign(company):
    n = _note(company, "SIM-swap campaign")
    week = [d for d in company["days"] if gen.CAMPAIGN[0].isoformat() <= d["date"] <= gen.CAMPAIGN[1].isoformat()]
    assert len(week) == 7 and n["date"] == gen.CAMPAIGN[1].isoformat()
    assert _claims(n) == {str(sum(d["sim_swap_holds"] for d in week)), "7"}


def test_activity_feed_has_no_unchecked_numbers(company):
    checked = (
        "Spanish",
        "Account takeover",
        "Social-engineering",
        "SIM-swap",
        "Card numbers",
        "Seven-day",
        "Tropical",
        "Card processor",
    )
    for n in company["notable"]:
        if n["title"].startswith(checked):
            continue
        assert _claims(n) <= {"100"}, n["title"]  # the rest are qualitative (or the audit's 100%)
        assert not re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d", n["detail"])


# ---------- compliance panel ----------


def test_recording_consent_is_asked_on_every_call_and_some_decline(company, calls, day):
    assert company["compliance"]["recording_consent_asked_rate"] == 1.0
    assert company["compliance"]["recording_consent_state"] == "Florida"
    for c in day:
        (g,) = [g for g in c["safeguards"] if g["control"] == "Recording consent"]
        assert g["result"].startswith("asked (Florida requires all-party consent)")
        assert ("declined" in g["result"]) == (c["recording"] == "declined")
    declined = [c for c in calls if c["recording"] == "declined"]
    assert len(declined) >= 3
    last30 = company["days"][-30:]
    rate = sum(d["recording_declined"] for d in last30) / sum(d["calls"] for d in last30)
    assert 0.03 < rate < 0.08
    assert abs(len(declined) / len(calls) - rate) < 0.03


# ---------- members and calls ----------


def test_one_phone_per_member(calls):
    phones: dict[str, set] = {}
    for c in calls:
        phones.setdefault(c["member"]["phone"], set()).add(c["member"]["name"])
    assert all(len(names) == 1 for names in phones.values())
    by_member = {}
    for c in calls:
        by_member.setdefault(c["member"]["name"], set()).add((c["member"]["phone"], c["member"]["member_no"]))
    assert all(len(v) == 1 for v in by_member.values())


def test_repeat_callers_are_realistic(calls):
    members = _members(calls)
    assert max(members.values()) <= 2
    assert sum(n > 1 for n in members.values()) / len(members) <= 0.08
    for key in members:
        resolved = [
            c["intent"]
            for c in calls
            if (c["member"]["name"], c["member"]["member_no"]) == key and c["outcome"] == "resolved"
        ]
        assert len(resolved) == len(set(resolved)), key


def test_social_engineers_claim_a_business_account_the_member_has(calls):
    for c in calls:
        if any("owner of the business account" in t for t in _texts(c, "member")):
            assert c["member"]["segment"] == "Business", c["id"]


def test_payment_transcripts_match_their_amounts(day):
    for c in day:
        pays = [a for a in c["actions"] if a["tool"] == "take_payment"]
        if not pays:
            continue
        member = _texts(c, "member")
        assert any(f"${pays[0]['amount_usd']:,.2f}" in t for t in member), c["id"]
        turns = [t for t in c["transcript"] if t["who"] != "system"]
        said = next(i for i, t in enumerate(turns) if t["who"] == "member" and "$" in t["text"])
        assert not any("How much" in t["text"] for t in turns[said:] if t["who"] == "agent"), c["id"]


def test_keypad_payments_never_mention_a_link(day):
    for c in day:
        statuses = {a["status"] for a in c["actions"] if a["tool"] == "take_payment"}
        words = " ".join(
            [g["result"] for g in c["safeguards"]] + _texts(c, "agent") + [a["detail"] for a in c["actions"]]
        )
        if "keypad_paid" in statuses:
            assert "link" not in words.lower(), c["id"]
        if "link_sent" in statuses:
            assert "keypad" not in words.lower(), c["id"]


def test_moving_changes_only_the_address(day):
    for c in day:
        for t in _texts(c, "member"):
            if re.search(r"\bmoved\b", t):
                assert "mailing address" in t and not re.search(r"email|phone", t), t


def test_transfer_reasons_fit_what_happened(day):
    threshold = sc.RISK_THRESHOLD
    for c in day:
        reason = c["transfer_reason"]
        assert (reason == sc.TRANSFER_REASONS["risk"]) == (c["risk_score"] >= threshold), c["id"]
        if reason == sc.TRANSFER_REASONS["unsupported"]:
            assert c["intent"] in ("Other",), c["id"]
            assert not any("wire" in t for t in _texts(c, "member")), c["id"]
        if any("locked the card" in t or "is locked" in t for t in _texts(c, "agent")):
            assert any("locked" in a["detail"] for a in c["actions"]), c["id"]
            assert any(g["control"] == "Policy gate" and "lock" in g["result"] for g in c["safeguards"]), c["id"]
        if any("wire" in t for t in _texts(c, "member")):
            assert reason == sc.TRANSFER_REASONS["policy"], c["id"]


def test_spanish_calls_can_go_to_the_bilingual_queue(day):
    spanish = [c for c in day if c["language"] == "es"]
    assert any(c["outcome"] == "transferred" for c in spanish)
    assert all(c["queue"] == "Bilingual member services" for c in spanish)


def test_abandoned_call_summaries_match_their_length(day):
    abandoned = [c for c in day if c["outcome"] == "abandoned"]
    assert abandoned and len({c["duration_s"] for c in abandoned}) > 1
    for c in abandoned:
        assert re.search(rf"after {c['duration_s']} seconds", c["summary"]), c["summary"]


def test_call_timing_is_realistic(day):
    gaps = []
    for c in day:
        turns = c["transcript"]
        assert turns[0]["t"] == 0 and turns[0]["text"].startswith("AI disclosure")
        consent = next(t for t in turns if t["text"].startswith("Recording consent"))
        assert consent["t"] >= 10, "the disclosure and the consent prompt take time to play"
        greet = next(i for i, t in enumerate(turns) if t["who"] == "agent")
        assert turns[greet]["t"] > consent["t"]
        if greet + 1 < len(turns) and turns[greet + 1]["who"] == "member":
            gaps.append(turns[greet + 1]["t"] - turns[greet]["t"] - sc._speech_seconds(turns[greet]["text"]))
    assert statistics.mean(gaps) <= 4, "members answer the greeting within a few seconds"


def test_bookings_and_hours_use_real_branches(day):
    branches = set(sc.BRANCHES) | set(sc.SPOKEN.values())
    for c in day:
        for a in c["actions"]:
            if a["tool"] == "book_meeting":
                assert a["branch"] in sc.BRANCHES and a["detail"].startswith(a["branch"])
        assert "Las Olas)" not in " ".join(_texts(c))
    assert {b["branch"] for b in gen.build()["branch_appointments"]} == set(sc.BRANCHES)
    assert len(sc.BRANCHES) == gen.COMPANY["branches"]
    hours = " ".join(t for c in day if c["intent"] == "Branch hours and locations" for t in _texts(c))
    assert any(b in hours for b in branches)


def test_summaries_are_written_per_call(calls):
    summaries = Counter(c["summary"] for c in calls)
    assert len(summaries) >= 0.7 * len(calls)
    assert summaries.most_common(1)[0][1] <= 10
    assert not any(s.endswith("Answered on the call.") for s in summaries)


# ---------- the calendar ----------


def test_sundays_and_the_holiday_are_all_after_hours(company, day):
    for d in company["days"]:
        when = date.fromisoformat(d["date"])
        if when.weekday() == 6 or when == gen.LABOR_DAY:
            assert d["after_hours"] == d["calls"], d["date"]
        else:
            share = sc.after_hours_share(when.weekday())
            assert abs(d["after_hours"] / d["calls"] - share) < 0.03, d["date"]
    assert company["days"][-1]["after_hours"] == sum(
        sc.is_after_hours(datetime.fromisoformat(c["started"])) for c in day
    )
    assert "closed Sunday" in gen.COMPANY["contact_center"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run demo/sample.js")
def test_the_console_moves_sample_dates_by_whole_weeks(company):
    """demo/sample.js shifts every sample date by whole weeks, so weekend and holiday dips keep their weekdays."""
    through = company["recent_calls"]["through"]
    nows = [
        "2026-10-07T23:59:59",
        "2026-10-08T09:00:00",
        "2026-10-14T10:00:00",
        "2027-02-03T12:00:00",
        "2026-12-31T23:00:00",
    ]
    script = (
        f"import {{weekShift}} from {str((ROOT / 'demo' / 'sample.js').as_uri())!r};"
        f"for (const n of {nows!r}) console.log(weekShift({through!r}, new Date(n)));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    last = datetime.fromisoformat(through)
    for now, shift in zip(nows, map(int, out.stdout.split()), strict=True):
        moved = last + timedelta(days=shift)
        assert shift % 7 == 0, (now, shift)
        assert moved <= datetime.fromisoformat(now) < moved + timedelta(days=7), (now, shift)


def test_docs_quote_the_call_list_as_generated(calls):
    text = (ROOT / "docs" / "console.md").read_text()
    spanish = sum(c["language"] == "es" for c in calls)
    assert f"the {len(calls)} most recent calls of its latest day" in text
    assert f"{spanish} of them in Spanish" in text
