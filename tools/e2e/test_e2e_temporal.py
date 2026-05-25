"""
E2E — Contradiction detection pipeline, auto-supersede, and as_of point-in-time queries.

Tests added for features implemented in Tier 2.3:
  - 3-layer contradiction detection (vector sim → heuristics → LLM arbitration)
  - Auto-supersede: directional contradictions call client.supersede() at write time
  - as_of: GET /memory/search?as_of=<ISO8601> returns the temporal snapshot at that instant

Each test uses an isolated namespace (via the `ns` fixture) and cleans up after the session.

Run against a live stack:
    # Against the dedicated E2E stack (recommended):
    make e2e-up && make e2e-run

    # Against the dev stack (quick iteration):
    ENGRAM_E2E_URL=http://localhost:8766 \
    ENGRAM_E2E_API_KEY=<your-key> \
    python -m pytest tools/e2e/test_e2e_temporal.py -v
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tools.e2e.conftest import content_list, search_memories, wait_for, write_memory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for as_of queries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _after_created_at(mem: dict, seconds: float = 1.0) -> str:
    """Return an ISO-8601 UTC string *seconds* after the server-reported created_at.

    Using the server's own created_at avoids host↔container clock-skew issues
    where the test machine clock is behind Docker's clock.
    """
    raw = mem.get("created_at", "")
    # Normalize: handle both "Z" suffix and "+00:00" offset
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    return (dt + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _contradiction_warnings(mem: dict) -> list[dict]:
    return mem.get("contradiction_warnings", [])


def _auto_superseded_ids(mem: dict) -> set[str]:
    return {
        w["existing_id"]
        for w in _contradiction_warnings(mem)
        if w.get("auto_superseded") is True
    }


# ---------------------------------------------------------------------------
# Contradiction detection pipeline — response-level assertions
# ---------------------------------------------------------------------------

class TestContradictionPipeline:
    """Verify the write response includes correct contradiction_warnings fields."""

    def test_negation_detected_fires_and_auto_supersedes(self, e2e_client, ns):
        old = write_memory(
            e2e_client,
            "The platform uses Redis for all session caching",
            ns,
            memory_type="fact",
        )
        new = write_memory(
            e2e_client,
            "The platform no longer uses Redis for caching — migrated to Memcached",
            ns,
            memory_type="fact",
        )
        warnings = _contradiction_warnings(new)
        assert len(warnings) >= 1, "Expected at least one contradiction warning"
        matching = [w for w in warnings if w["existing_id"] == old["id"]]
        assert matching, f"Warning for old memory {old['id']} not found in {warnings}"
        w = matching[0]
        assert w["direction"] in ("negation_detected", "topic_update", "opposite_polarity", "llm_confirmed"), \
            f"Unexpected direction: {w['direction']}"
        assert w["auto_superseded"] is True, "Directional contradiction should be auto-superseded"
        assert w["similarity"] > 0.60

    def test_topic_update_fires_and_auto_supersedes(self, e2e_client, ns):
        # 3 shared prefix words: "the auth refactor"
        old = write_memory(
            e2e_client,
            "the auth refactor: not yet started. Planned for next quarter.",
            ns,
            memory_type="fact",
        )
        new = write_memory(
            e2e_client,
            "the auth refactor: completed successfully. Merged to main on 2026-05-20.",
            ns,
            memory_type="fact",
        )
        warnings = _contradiction_warnings(new)
        matching = [w for w in warnings if w["existing_id"] == old["id"]]
        assert matching, "Expected contradiction warning for topic_update"
        w = matching[0]
        assert w["direction"] in ("topic_update", "negation_detected", "llm_confirmed")
        assert w["auto_superseded"] is True

    def test_opposite_polarity_fires_and_auto_supersedes(self, e2e_client, ns):
        old = write_memory(
            e2e_client,
            "We should always enable the dark mode feature flag for all users",
            ns,
            memory_type="fact",
        )
        new = write_memory(
            e2e_client,
            "We should never enable the dark mode feature flag — rollback immediately",
            ns,
            memory_type="fact",
        )
        warnings = _contradiction_warnings(new)
        matching = [w for w in warnings if w["existing_id"] == old["id"]]
        assert matching, "Expected contradiction warning for opposite polarity"
        w = matching[0]
        assert w["direction"] in ("negation_detected", "opposite_polarity", "llm_confirmed")
        assert w["auto_superseded"] is True

    def test_no_contradiction_for_unrelated_memories(self, e2e_client, ns):
        write_memory(e2e_client, "The office coffee machine is broken", ns, memory_type="fact")
        new = write_memory(
            e2e_client,
            "Kubernetes deployment strategy uses rolling updates",
            ns,
            memory_type="fact",
        )
        warnings = _contradiction_warnings(new)
        # Unrelated topics — zero contradiction warnings expected
        assert len(warnings) == 0, f"False positive: {warnings}"

    def test_noise_tagged_memory_skipped_as_source(self, e2e_client, ns):
        # session-log tagged memories are observations, not claims — skipped
        write_memory(
            e2e_client,
            "use Redis for all caching layers — session note",
            ns,
            memory_type="fact",
            tags=["session-log"],
        )
        new = write_memory(
            e2e_client,
            "do not use Redis, migrating to Memcached for all caching",
            ns,
            memory_type="fact",
        )
        warnings = _contradiction_warnings(new)
        # session-log source should be skipped — no warning expected
        assert len(warnings) == 0, \
            f"session-log memory should not be a contradiction source: {warnings}"

    def test_new_noise_tagged_write_skips_detection(self, e2e_client, ns):
        # New memory with session-log tag should skip detection entirely
        write_memory(
            e2e_client,
            "The API uses REST endpoints for all services",
            ns,
            memory_type="fact",
        )
        new = write_memory(
            e2e_client,
            "The API no longer uses REST — migrated to GraphQL",
            ns,
            memory_type="fact",
            tags=["session-log"],  # new memory is ephemeral — skip detection
        )
        # The response should have no warnings (detection skipped entirely)
        warnings = _contradiction_warnings(new)
        assert len(warnings) == 0

    def test_contradiction_warning_fields_complete(self, e2e_client, ns):
        write_memory(e2e_client, "Use PostgreSQL for all transactional data", ns, memory_type="decision")
        new = write_memory(
            e2e_client,
            "Do not use PostgreSQL — migrating to CockroachDB for distributed transactions",
            ns,
            memory_type="decision",
        )
        warnings = _contradiction_warnings(new)
        if not warnings:
            pytest.skip("No contradiction detected — cannot verify warning fields")
        w = warnings[0]
        assert "existing_id" in w and w["existing_id"]
        assert "existing_content" in w and w["existing_content"]
        assert "similarity" in w and 0.0 < w["similarity"] <= 1.0
        assert "direction" in w and w["direction"]
        assert "auto_superseded" in w
        assert "reason" in w and w["reason"]


# ---------------------------------------------------------------------------
# Auto-supersede search behavior — old hidden, new visible
# ---------------------------------------------------------------------------

class TestAutoSupersede:
    """After auto-supersede, the old memory must not appear in current searches."""

    def test_superseded_memory_hidden_from_search(self, e2e_client, ns):
        token = f"autosup-{uuid.uuid4().hex[:8]}"
        write_memory(
            e2e_client,
            f"auth service {token}: uses basic password auth for all endpoints",
            ns,
            memory_type="fact",
        )
        write_memory(
            e2e_client,
            f"auth service {token}: no longer uses basic auth — migrated to OAuth2 JWT",
            ns,
            memory_type="fact",
        )
        # Allow a brief moment for index consistency
        time.sleep(0.5)
        results = search_memories(e2e_client, f"auth service {token}", ns, top_k=10)
        contents = content_list(results)
        assert not any("basic password auth" in c for c in contents), \
            "Auto-superseded memory should not appear in current search"

    def test_new_memory_visible_after_supersede(self, e2e_client, ns):
        token = f"vis-{uuid.uuid4().hex[:8]}"
        write_memory(
            e2e_client,
            f"cache layer {token}: uses Memcached for session storage",
            ns,
            memory_type="fact",
        )
        write_memory(
            e2e_client,
            f"cache layer {token}: no longer uses Memcached — switched to Redis Cluster",
            ns,
            memory_type="fact",
        )
        time.sleep(0.5)
        results = search_memories(e2e_client, f"cache layer {token}", ns, top_k=5)
        contents = content_list(results)
        assert any("Redis Cluster" in c for c in contents), \
            "New memory should appear in search after supersede"

    def test_get_by_id_still_returns_superseded_memory(self, e2e_client, ns):
        token = f"byid-{uuid.uuid4().hex[:8]}"
        old = write_memory(
            e2e_client,
            f"service {token}: timeout set to 30 seconds",
            ns,
            memory_type="fact",
        )
        write_memory(
            e2e_client,
            f"service {token}: timeout no longer 30s — updated to 60 seconds",
            ns,
            memory_type="fact",
        )
        # GET by ID should still return the superseded record (it's not deleted)
        r = e2e_client.get(f"/api/v1/memory/{old['id']}", params={"ns": ns})
        assert r.status_code == 200, f"Superseded memory should still be fetchable by ID: {r.text}"
        data = r.json()
        assert data["id"] == old["id"]

    def test_delete_still_works_independently(self, e2e_client, ns):
        mem = write_memory(e2e_client, "standalone memory to delete", ns)
        r = e2e_client.delete(f"/api/v1/memory/{mem['id']}", params={"ns": ns})
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# as_of point-in-time queries
# ---------------------------------------------------------------------------

class TestPointInTime:
    """as_of returns the temporal snapshot: memories active at that instant."""

    def test_as_of_between_writes_returns_old_state(self, e2e_client, ns):
        token = f"pit-{uuid.uuid4().hex[:8]}"

        # Write old memory
        write_memory(
            e2e_client,
            f"config {token}: rate limit set to 100 requests per minute",
            ns,
            memory_type="fact",
        )
        time.sleep(1)  # ensure a distinct timestamp gap
        t_between = _now_iso()
        time.sleep(1)

        # Write new memory that supersedes the old
        write_memory(
            e2e_client,
            f"config {token}: rate limit no longer 100 — updated to 500 requests per minute",
            ns,
            memory_type="fact",
        )
        time.sleep(0.5)

        # Current search: should return the new memory
        current = search_memories(e2e_client, f"config {token} rate limit", ns, top_k=5)
        current_contents = content_list(current)
        assert any("500" in c for c in current_contents), \
            f"Current search should return new 500 req/min memory. Got: {current_contents}"
        assert not any("100 requests per minute" in c and "no longer" not in c
                        for c in current_contents), \
            "Old 100 req/min memory should be hidden in current search"

        # Point-in-time search: t_between is after old write, before new write
        pit = search_memories(
            e2e_client, f"config {token} rate limit", ns, top_k=5, as_of=t_between
        )
        pit_contents = content_list(pit)
        assert any("100 requests per minute" in c for c in pit_contents), \
            f"as_of={t_between} should return old 100 req/min memory. Got: {pit_contents}"

    def test_as_of_before_any_write_returns_empty(self, e2e_client, ns):
        t_before = _now_iso()
        time.sleep(1)
        token = f"future-{uuid.uuid4().hex[:8]}"
        write_memory(e2e_client, f"new fact written after t_before: {token}", ns)
        time.sleep(0.3)

        results = search_memories(
            e2e_client, token, ns, top_k=5, as_of=t_before
        )
        contents = content_list(results)
        assert not any(token in c for c in contents), \
            f"Memory written after as_of should not appear. Got: {contents}"

    def test_as_of_includes_active_memories(self, e2e_client, ns):
        token = f"active-{uuid.uuid4().hex[:8]}"
        # Use semantically rich content so the embedding scores well against the query
        content = f"The Kubernetes deployment strategy uses rolling updates for {token} service"
        mem = write_memory(e2e_client, content, ns)
        # Use server-reported created_at + 1s to avoid host↔container clock-skew
        t_after = _after_created_at(mem, seconds=1.0)

        results = search_memories(
            e2e_client, f"Kubernetes deployment rolling updates {token}", ns, top_k=5, as_of=t_after
        )
        contents = content_list(results)
        assert any(token in c for c in contents), \
            f"Active memory should appear in as_of=now query. Got: {contents}"

    def test_as_of_chain_three_supersessions(self, e2e_client, ns):
        token = f"chain-{uuid.uuid4().hex[:8]}"

        write_memory(
            e2e_client,
            f"service {token}: running version alpha in staging environment",
            ns,
            memory_type="fact",
        )
        time.sleep(1)
        t1 = _now_iso()
        time.sleep(1)

        # v2: supersedes v1 (negation of "running version alpha")
        write_memory(
            e2e_client,
            f"service {token}: no longer running version alpha — upgraded to version beta",
            ns,
            memory_type="fact",
        )
        time.sleep(1)
        t2 = _now_iso()
        time.sleep(1)

        # v3: supersedes v2
        write_memory(
            e2e_client,
            f"service {token}: no longer on version beta — rolled back to version alpha",
            ns,
            memory_type="fact",
        )
        time.sleep(0.5)

        # t1: only v1 (alpha) exists
        pit1 = content_list(search_memories(e2e_client, f"service {token} version", ns, top_k=5, as_of=t1))
        assert any("alpha" in c and "no longer" not in c for c in pit1), \
            f"At t1 should see alpha (v1). Got: {pit1}"

        # t2: v1 superseded, v2 (beta) active
        pit2 = content_list(search_memories(e2e_client, f"service {token} version", ns, top_k=5, as_of=t2))
        assert any("beta" in c for c in pit2), \
            f"At t2 should see beta (v2). Got: {pit2}"

    def test_as_of_invalid_format_returns_422(self, e2e_client, ns):
        r = e2e_client.get("/api/v1/memory/search", params={
            "q": "anything",
            "ns": ns,
            "as_of": "not-a-date",
        })
        assert r.status_code == 422, \
            f"Invalid as_of format should return 422 Unprocessable Entity, got {r.status_code}"

    def test_as_of_vector_mode_returns_temporal_snapshot(self, e2e_client, ns):
        token = f"vec-{uuid.uuid4().hex[:8]}"
        write_memory(e2e_client, f"vector test {token}: original value is alpha", ns)
        time.sleep(1)
        t_mid = _now_iso()
        time.sleep(1)
        write_memory(
            e2e_client,
            f"vector test {token}: original value no longer alpha — changed to beta",
            ns,
        )
        time.sleep(0.3)

        pit = search_memories(
            e2e_client, f"vector test {token}", ns, top_k=5, mode="vector", as_of=t_mid
        )
        contents = content_list(pit)
        assert any("alpha" in c and "no longer" not in c for c in contents), \
            f"vector mode as_of should return pre-supersede state. Got: {contents}"


# ---------------------------------------------------------------------------
# Review-due
# ---------------------------------------------------------------------------

class TestReviewDue:
    """Memories past their review_by date surface on GET /memory/review-due."""

    def test_past_review_by_appears_in_review_due(self, e2e_client, ns):
        write_memory(
            e2e_client,
            "Temporary workaround for login bug",
            ns,
            review_by="2020-06-01T00:00:00Z",
            tags=["workaround"],
        )
        r = e2e_client.get("/api/v1/memory/review-due", params={"ns": ns, "limit": 20})
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and len(items) >= 1
        contents = [m.get("content", "") for m in items]
        assert any("Temporary workaround" in c for c in contents)

    def test_future_review_by_not_in_review_due(self, e2e_client, ns):
        write_memory(
            e2e_client,
            "Future review memory — not yet due",
            ns,
            review_by="2099-01-01T00:00:00Z",
        )
        r = e2e_client.get("/api/v1/memory/review-due", params={"ns": ns, "limit": 20})
        assert r.status_code == 200
        items = r.json()
        contents = [m.get("content", "") for m in items]
        assert not any("Future review memory" in c for c in contents)

    def test_review_due_respects_limit(self, e2e_client, ns):
        for i in range(5):
            write_memory(
                e2e_client,
                f"Past-due memory #{i}",
                ns,
                review_by="2019-01-01T00:00:00Z",
            )
        r = e2e_client.get("/api/v1/memory/review-due", params={"ns": ns, "limit": 3})
        assert r.status_code == 200
        items = r.json()
        assert len(items) <= 3
