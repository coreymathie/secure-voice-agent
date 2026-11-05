# Corey Mathie, 2026
"""Money and figure formats in the console: en-US with a leading "$" under any locale, kept out of translation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "demo"


def _js_files() -> list[Path]:
    return sorted(DEMO.glob("*.js"))


def test_no_currency_style_or_usd_code_in_any_formatter():
    for path in _js_files():
        src = path.read_text()
        assert 'style: "currency"' not in src, f"{path.name} formats money with Intl currency style"
        assert not re.search(r"currency:\s*\"USD\"", src), f"{path.name} formats money as a USD code"


def test_money_is_formatted_in_one_module():
    """Every "$" + number formatter lives in demo/format.js, so one test covers them all."""
    for path in _js_files():
        if path.name == "format.js":
            continue
        assert not re.search(r"\"\$\"\s*\+", path.read_text()), f"{path.name} builds a money string itself"


def test_figure_containers_are_not_translated():
    app = (DEMO / "app.js").read_text()
    assert '<div class="v" translate="no">' in app and '<div class="s" translate="no">' in app
    assert 'class="kpi-foot" translate="no"' in app
    charts = (DEMO / "charts.js").read_text()
    assert charts.count('translate: "no"') == 3 and 'class="spark" translate="no"' in charts
    html = (DEMO / "index.html").read_text()
    assert 'class="notranslate"' not in html and 'translate="no"' not in html.split("<body", 1)[1].split(">", 1)[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run demo/format.js")
def test_formats_under_a_french_locale():
    script = (
        f"import * as f from {json.dumps((DEMO / 'format.js').as_uri())};"
        "console.log(JSON.stringify(["
        "f.usd(940), f.usd(1100), f.usd(180.5), f.usdCents(180), f.moneyShort(1.5e6), f.moneyShort(1462000),"
        "f.moneyShort(142369), f.moneyShort(1.4e9), f.unitPrice(8.01), f.unitPrice(0.44), f.unitPrice(0.5),"
        "f.num(92400)]))"
    )
    env = {**os.environ, "LANG": "fr_FR.UTF-8", "LC_ALL": "fr_FR.UTF-8"}
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, env=env
    )
    got = json.loads(out.stdout)
    assert got == [
        "$940",
        "$1,100",
        "$180.50",
        "$180.00",
        "$1.5M",
        "$1.46M",
        "$142K",
        "$1.4B",
        "$8.01",
        "$0.44",
        "$0.50",
        "92,400",
    ]
    assert not any("USD" in g for g in got)
