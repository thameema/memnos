"""
tools/conftest.py — Shared pytest fixtures for the tools/ test suite.

The `runner` fixture is used by integration-style test scripts
(test_agent_communication.py, test_decision_coverage.py,
test_epoch_ms_temporal.py) that define their own Runner dataclass.
When the engram API is not available, all such tests are automatically
skipped rather than erroring.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")


@pytest.fixture(scope="session")
def runner():
    """Provide a minimal Runner-compatible namespace for integration tests.

    Skips the entire test session if the engram API is not reachable.
    Attributes mirror the union of all Runner dataclasses across the tool
    test scripts so any of them can use this fixture without modification.
    """
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(
                f"{ENGRAM_API}/api/v1/admin/health",
                headers={"X-API-Key": ENGRAM_KEY},
            )
            if r.status_code != 200:
                pytest.skip(
                    f"engram API not healthy (HTTP {r.status_code}) — "
                    "set ENGRAM_API_URL to override"
                )
    except Exception as exc:
        pytest.skip(
            f"engram API not reachable at {ENGRAM_API}: {exc} — "
            "set ENGRAM_API_URL to override"
        )

    return SimpleNamespace(
        verbose=False,
        only=None,
        skip_webhook=True,
        results=[],
    )
