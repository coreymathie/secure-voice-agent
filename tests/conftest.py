# Corey Mathie, 2026
"""Unit tests call Lambda handlers directly, so they run with unsigned requests allowed.

Signature checking itself is covered in tests/test_request_signing.py, and the
call evals (evals/harness.py) send signed requests the way the voice process does.
"""

import pytest


@pytest.fixture(autouse=True)
def _allow_unsigned_tool_calls(monkeypatch):
    monkeypatch.delenv("TOOL_API_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_TOOL_CALLS", "true")
