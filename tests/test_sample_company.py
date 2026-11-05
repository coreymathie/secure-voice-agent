# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
import re
from pathlib import Path

from scripts import generate_sample_company as gen

ROOT = Path(__file__).resolve().parents[1]


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_generation_is_deterministic():
    assert gen.render(gen.build()) == gen.render(gen.build())


def test_daily_outcomes_add_up():
    data = gen.build()
    assert len(data["days"]) == gen.DAYS
    for d in data["days"]:
        assert d["contained"] + d["transferred"] + d["abandoned"] == d["calls"]
        assert 0 <= d["after_hours"] <= d["calls"]
        assert d["stepup_passed"] <= d["stepups"]
        assert d["card_numbers_scrubbed"] <= d["pii_scrubbed"]
        assert 0 <= d["recording_declined"] <= d["calls"]
        # Fraud attempts are the three fraud controls; verification lockouts are counted on their own.
        assert d["fraud_blocked"] == d["sim_swap_holds"] + d["social_engineering_handoffs"] + d["takeover_patterns"]


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    data = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())
    assert data["company"]["fictional"] is True
    assert "not measurements" in data["disclaimer"]
    assert data["assumptions"]["agent_cost_per_call_usd"] > data["assumptions"]["ai_cost_per_call_usd"]


def test_recent_calls_are_reproducible_and_on_the_last_day():
    data = gen.build_calls()
    assert gen.render_calls(data) == gen.render_calls(gen.build_calls())
    calls = data["calls"]
    assert len(calls) == gen.RECENT_CALLS
    starts = [c["started"] for c in calls]
    assert starts == sorted(starts, reverse=True)
    through = gen.build()["recent_calls"]["through"]
    assert starts[0] == through
    assert all(s.startswith(gen.END.isoformat()) and s <= through for s in starts)
    assert len({c["id"] for c in calls}) == len(calls)


def test_recent_calls_use_only_fictional_contact_details():
    text = gen.render_calls(gen.build_calls())
    assert not re.search(r"\b\d{13,16}\b", text.replace(" ", "")), "no full card numbers"
    for c in gen.build_calls()["calls"]:
        assert re.fullmatch(r"\(\d{3}\) 555-01\d\d", c["member"]["phone"])
    assert set(re.findall(r"@([\w.-]+)", text)) <= {"example.com"}


def test_recent_calls_match_the_dashboard():
    company = gen.build()
    calls = gen.build_calls()["calls"]
    intents = {i["intent"] for i in company["intents"]}
    assert {c["intent"] for c in calls} <= intents
    resolved = sum(c["outcome"] == "resolved" for c in calls) / len(calls)
    last = company["days"][-1]
    assert abs(resolved - last["contained"] / last["calls"]) < 0.08
    for c in calls:
        assert (c["outcome"] == "transferred") == bool(c["transfer_reason"])
        assert c["transcript"][0]["who"] == "system", "the AI disclosure comes first"
        assert c["safeguards"][-1]["control"] == "Audit log"
        if "Fraud stopped" in c["flags"]:
            assert c["outcome"] == "transferred"
            assert not any(a["tool"] == "take_payment" for a in c["actions"])


# ---------- How these sample numbers were set ----------


def test_every_headline_figure_has_a_benchmark_row():
    data = gen.build()
    figures = [b["figure"] for b in data["benchmarks"]]
    assert len(figures) == len(set(figures)) == len(gen.BENCHMARKS) >= 10
    for b in data["benchmarks"]:
        assert b["value"] and b["basis"] and b["status"] in ("cited", "derived", "assumption"), b


def test_every_cited_figure_has_a_url_and_the_quoted_text():
    """A cited row needs title, publisher, year, URL, access date and the text the figure comes from, per source."""
    rows = gen.build()["benchmarks"]
    assert sum(b["status"] == "cited" for b in rows) >= 1
    for b in rows:
        if b["status"] == "cited":
            assert b["benchmark"] and b["sources"], b["figure"]
            for src in b["sources"]:
                assert src["url"].startswith("https://"), b["figure"]
                assert src["title"] and src["publisher"] and str(src["year"]).isdigit() and src["quote"], b["figure"]
                assert src["accessed"] == gen.ACCESSED
        else:
            assert b["sources"] == [] and b["benchmark"] is None, b["figure"]
            assert b["label"] == (gen.DERIVED if b["status"] == "derived" else gen.UNVERIFIED), b["figure"]


def test_cited_values_sit_inside_their_published_range():
    values = gen.benchmark_values(gen.build()["days"])
    ranged = [b for b in gen.BENCHMARKS if "range" in b]
    assert ranged
    for b in ranged:
        assert b.get("sources"), b["figure"]
        low, high = b["range"]
        assert low <= values[b["key"]][1] <= high, (b["figure"], values[b["key"]], b["range"])


def test_ai_cost_per_call_matches_the_list_prices():
    days = gen.build()["days"]
    assert gen.ASSUMPTIONS["ai_cost_per_call_usd"] == gen.ai_cost_per_call(days)
    assert round(sum(gen.AI_PRICE_PER_MINUTE_USD.values()), 4) == 0.3264
    ai = next(b for b in gen.BENCHMARKS if b["key"] == "ai_cost")
    quoted = " ".join(s["quote"] for s in ai["sources"])
    prices = ("$0.06 per minute of audio input", "$0.24 per minute of audio output", "$0.0220/ min", "$0.0044/ min")
    for price in prices:
        assert price in quoted


def test_cost_figures_match_the_cited_sources():
    a = gen.ASSUMPTIONS
    agent = next(b for b in gen.BENCHMARKS if b["key"] == "agent_cost")
    assert f"${a['agent_cost_per_call_usd']:.2f}" in agent["sources"][0]["quote"]
    company = gen.build()["company"]
    assets = next(b for b in gen.BENCHMARKS if b["key"] == "assets_per_member")
    assert f"${company['assets_usd'] / 1e9:.1f}B" in assets["basis"]


def test_docs_table_matches_the_data():
    doc = (gen.ROOT / "docs" / "console.md").read_text()
    table = doc.split("<!-- benchmarks:start -->\n", 1)[1].split("\n<!-- benchmarks:end -->", 1)[0]
    assert table == gen.benchmarks_markdown(gen.build()["benchmarks"])
