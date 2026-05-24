"""
E2E test fixtures — connect to the live test stack on localhost:18766.

Start the stack first:
    make e2e-up

Or run the full suite (up → test → down):
    make e2e

The test stack runs on ports 18765 (MCP) and 18766 (REST API) with data in
~/.engram-test — it never touches your production ~/.engram directory.

All tests receive a unique `ns` fixture (e2e:test:<uuid4>) so each test run
has its own isolated namespace. Memories are cleaned up after the session.
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

# ── Test stack connection ─────────────────────────────────────────────────────
E2E_BASE_URL = os.environ.get("ENGRAM_E2E_URL", "http://localhost:18766")
E2E_API_KEY  = os.environ.get("ENGRAM_E2E_API_KEY", "test-api-key-e2e")
E2E_TIMEOUT  = 30.0

_HEADERS = {"X-API-Key": E2E_API_KEY, "Content-Type": "application/json"}


# ── Session-scoped client ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def e2e_client():
    """httpx client pointed at the test stack. Skips the session if unhealthy."""
    client = httpx.Client(
        base_url=E2E_BASE_URL,
        headers=_HEADERS,
        timeout=E2E_TIMEOUT,
    )
    # Health gate — skip all e2e tests if the stack is not running
    try:
        r = client.get("/api/v1/admin/health", timeout=5)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Test stack not reachable at {E2E_BASE_URL} ({exc}).\n"
            "Run `make e2e-up` to start it."
        )
    yield client
    client.close()


@pytest.fixture(scope="session")
def e2e_async_client():
    """Async httpx client for tests that need async calls."""
    import asyncio

    async def _get():
        async with httpx.AsyncClient(
            base_url=E2E_BASE_URL,
            headers=_HEADERS,
            timeout=E2E_TIMEOUT,
        ) as c:
            yield c

    # Not directly usable in sync tests — use e2e_client there.
    # Provided for async test functions that use `async for`.
    return _get


# ── Per-test isolated namespace ───────────────────────────────────────────────

_session_ns_prefix = f"e2e:test:{uuid.uuid4().hex[:8]}"
_created_namespaces: list[str] = []


@pytest.fixture()
def ns(e2e_client):
    """Return a unique namespace for this test. Cleaned up after the session."""
    test_ns = f"{_session_ns_prefix}:{uuid.uuid4().hex[:6]}"
    _created_namespaces.append(test_ns)
    return test_ns


@pytest.fixture(scope="session", autouse=True)
def _cleanup_namespaces(e2e_client):
    """Delete all test namespaces after the full session."""
    yield
    for test_ns in _created_namespaces:
        try:
            e2e_client.delete(f"/api/v1/admin/namespaces/{test_ns}")
        except Exception:
            pass  # best-effort cleanup


# ── Convenience helpers ───────────────────────────────────────────────────────

def write_memory(client: httpx.Client, content: str, namespace: str, **kwargs) -> dict:
    """POST /api/v1/memory/ and return the created memory dict."""
    payload = {"content": content, "namespace": namespace, **kwargs}
    r = client.post("/api/v1/memory/", json=payload)
    assert r.status_code in (200, 201), f"write_memory failed: {r.status_code} {r.text}"
    return r.json()


def search_memories(client: httpx.Client, query: str, namespace: str, top_k: int = 5) -> list[dict]:
    """GET /api/v1/memory/search and return results list."""
    r = client.get("/api/v1/memory/search", params={"q": query, "ns": namespace, "top_k": top_k})
    assert r.status_code == 200, f"search failed: {r.status_code} {r.text}"
    data = r.json()
    return data if isinstance(data, list) else data.get("results", [])


def wait_for(fn, timeout: float = 10.0, interval: float = 0.5, msg: str = "condition") -> None:
    """Poll fn() until truthy or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for: {msg}")
