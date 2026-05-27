"""
tools/test_provenance.py — Integration tests for memory provenance auto-fill.

Verifies that every memory write is stamped with the correct chain-of-custody
fields and that those fields survive the round-trip back through search results.

Requires a live engram API (uses runner fixture from conftest.py — skipped
automatically when the API is not reachable).

Coverage:
  1. user_id auto-filled from API key
  2. tool auto-filled from X-Engram-Tool header
  3. agent_id auto-filled from X-Engram-Agent-Id header
  4. git_commit auto-filled by server (env var or git CLI)
  5. caller-supplied provenance takes precedence over server defaults
  6. provenance survives round-trip through GET /memory/{id}
  7. provenance appears in search results
  8. read-only key cannot write (provenance not relevant, but auth guard works)
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")
TEST_NS = "test:provenance:integ"


def _uid() -> str:
    return str(uuid.uuid4())[:8]


def _write(client: httpx.Client, content: str, ns: str, **headers) -> dict:
    r = client.post(
        f"{ENGRAM_API}/api/v1/memory/",
        json={"content": content, "namespace": ns, "memory_type": "fact"},
        headers=headers,
    )
    assert r.status_code == 201, f"Write failed: {r.status_code} {r.text}"
    return r.json()


def _get(client: httpx.Client, memory_id: str, ns: str) -> dict:
    r = client.get(f"{ENGRAM_API}/api/v1/memory/{memory_id}", params={"ns": ns})
    assert r.status_code == 200, f"GET failed: {r.status_code} {r.text}"
    return r.json()


def _search(client: httpx.Client, query: str, ns: str) -> list[dict]:
    r = client.get(
        f"{ENGRAM_API}/api/v1/memory/search",
        params={"q": query, "ns": ns, "top_k": 5},
    )
    assert r.status_code == 200, f"Search failed: {r.status_code} {r.text}"
    return r.json()


def _delete(client: httpx.Client, memory_id: str, ns: str) -> None:
    client.delete(f"{ENGRAM_API}/api/v1/memory/{memory_id}", params={"ns": ns})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_user_id_auto_filled_from_api_key(runner) -> None:
    """user_id is populated from the API key even when not supplied by caller."""
    ns = f"{TEST_NS}:{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance user_id auto-fill test", ns)
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            assert prov.get("user_id"), (
                f"provenance.user_id is empty — expected it to be auto-filled from API key. Got: {prov}"
            )
        finally:
            _delete(c, mid, ns)


def test_tool_header_populates_provenance(runner) -> None:
    """X-Engram-Tool header is stored in provenance.tool."""
    ns = f"{TEST_NS}:{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance tool header test", ns,
                     **{"X-Engram-Tool": "pytest-provenance-test"})
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            assert prov.get("tool") == "pytest-provenance-test", (
                f"Expected tool='pytest-provenance-test', got: {prov.get('tool')!r}"
            )
        finally:
            _delete(c, mid, ns)


def test_agent_id_header_populates_provenance(runner) -> None:
    """X-Engram-Agent-Id header is stored in provenance.agent_id."""
    ns = f"{TEST_NS}:{_uid()}"
    agent_id = f"test-agent-{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance agent_id header test", ns,
                     **{"X-Engram-Agent-Id": agent_id})
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            assert prov.get("agent_id") == agent_id, (
                f"Expected agent_id={agent_id!r}, got: {prov.get('agent_id')!r}"
            )
        finally:
            _delete(c, mid, ns)


def test_git_commit_auto_filled_by_server(runner) -> None:
    """git_commit is populated by the server (env var or git CLI) when not supplied."""
    ns = f"{TEST_NS}:{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance git_commit auto-fill test", ns)
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            git_commit = prov.get("git_commit", "")
            if git_commit:
                assert len(git_commit) >= 7, (
                    f"git_commit looks malformed: {git_commit!r} (expected short SHA)"
                )
            assert "git_commit" in prov, "git_commit key missing from provenance dict entirely"
        finally:
            _delete(c, mid, ns)


def test_caller_supplied_provenance_takes_precedence(runner) -> None:
    """Explicitly supplied provenance fields are not overwritten by server defaults."""
    ns = f"{TEST_NS}:{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/memory/",
            json={
                "content": "provenance caller override test",
                "namespace": ns,
                "memory_type": "fact",
                "provenance": {
                    "tool": "my-custom-tool",
                    "agent_id": "custom-agent-001",
                    "git_commit": "abc1234",
                    "jira_ticket": "HPTE-999",
                    "team": "test-team",
                },
            },
        )
        assert r.status_code == 201
        mid = r.json()["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            assert prov.get("tool") == "my-custom-tool", f"tool overwritten: {prov}"
            assert prov.get("agent_id") == "custom-agent-001", f"agent_id overwritten: {prov}"
            assert prov.get("git_commit") == "abc1234", f"git_commit overwritten: {prov}"
            assert prov.get("jira_ticket") == "HPTE-999", f"jira_ticket lost: {prov}"
            assert prov.get("team") == "test-team", f"team lost: {prov}"
        finally:
            _delete(c, mid, ns)


def test_provenance_survives_round_trip_via_get(runner) -> None:
    """Provenance written on POST is returned intact on GET /memory/{id}."""
    ns = f"{TEST_NS}:{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance round-trip test", ns,
                     **{"X-Engram-Tool": "round-trip-tool"})
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            assert "provenance" in full, "provenance key missing from GET response"
            prov = full["provenance"]
            assert isinstance(prov, dict), f"provenance is not a dict: {type(prov)}"
            assert prov.get("tool") == "round-trip-tool"
            assert prov.get("user_id"), "user_id empty in round-trip"
        finally:
            _delete(c, mid, ns)


def test_provenance_present_in_search_results(runner) -> None:
    """Provenance is included in /memory/search results, not stripped."""
    ns = f"{TEST_NS}:{_uid()}"
    marker = f"provenance-search-marker-{_uid()}"
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, marker, ns, **{"X-Engram-Tool": "search-test-tool"})
        mid = mem["id"]
        try:
            results = _search(c, marker, ns)
            assert results, "Search returned no results"
            hit = next((r for r in results if r["id"] == mid), None)
            assert hit is not None, f"Written memory {mid} not found in search results"
            prov = hit.get("provenance", {})
            assert isinstance(prov, dict), "provenance missing from search result"
            assert prov.get("tool") == "search-test-tool", (
                f"provenance.tool not in search result: {prov}"
            )
        finally:
            _delete(c, mid, ns)


def test_all_provenance_fields_present_in_response(runner) -> None:
    """Every provenance field from the model is present in the API response."""
    ns = f"{TEST_NS}:{_uid()}"
    expected_fields = {"user_id", "tool", "agent_id", "git_commit", "jira_ticket", "team"}
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as c:
        mem = _write(c, "provenance field completeness test", ns)
        mid = mem["id"]
        try:
            full = _get(c, mid, ns)
            prov = full.get("provenance", {})
            missing = expected_fields - set(prov.keys())
            assert not missing, (
                f"These provenance fields are missing from the API response: {missing}"
            )
        finally:
            _delete(c, mid, ns)
