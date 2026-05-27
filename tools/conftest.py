"""
tools/conftest.py — Shared pytest fixtures for the tools/ test suite.

The `runner` fixture is used by integration-style test scripts
(test_agent_communication.py, test_decision_coverage.py,
test_epoch_ms_temporal.py, test_arcadedb.py) that define their own Runner
dataclass.  When the engram API is not available, all such tests are
automatically skipped rather than erroring.
"""
from __future__ import annotations

# smoke_test.py is a standalone runner script; its test_* functions take a
# requests.Session param that is not a pytest fixture, so exclude it.
collect_ignore = ["smoke_test.py"]

import os
from types import SimpleNamespace

import httpx
import pytest

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")


def _get_openai():
    """Lazy OpenAI client loader — returns None when key is absent."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("OPENAI_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key)
    except ImportError:
        return None


_openai_client = None


def _get_openai_cached():
    global _openai_client
    if _openai_client is None:
        _openai_client = _get_openai()
    return _openai_client


@pytest.fixture(scope="session")
def runner():
    """Provide a minimal Runner-compatible namespace for integration tests.

    Skips the entire test session if the engram API is not reachable.
    Attributes mirror the union of all Runner dataclasses across the tool
    test scripts so any of them can use this fixture without modification.
    Includes get_openai() to satisfy test_arcadedb.py's TestRunner interface.
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
        get_openai=_get_openai_cached,
    )
