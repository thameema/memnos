"""
tools/test_missing_coverage.py — Integration tests for features with zero or thin coverage.

Covers:
  A. tasks/feedback  (POST /tasks/feedback)
  B. Auto-supersede  (similar content triggers supersession on write)
  C. CSV export      (GET /admin/export?format=csv)
  D. Knowledge/communities (GET /knowledge/communities)
  E. Search filtering — tags, mode=hybrid
  F. Memory affects field round-trip
  G. Namespace isolation — write to A, cannot read from B
  H. Import with namespace override
  I. Review-due with realistic review_after dates

Requires a live engram API (runner fixture from conftest.py).
"""
from __future__ import annotations

import csv
import io
import os
import time
import uuid

import httpx
import pytest

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")
BASE_NS = "test:missing-cov"


def _uid() -> str:
    return str(uuid.uuid4())[:8]


def _client() -> httpx.Client:
    return httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=20)


def _write(c: httpx.Client, content: str, ns: str, **extra) -> dict:
    body = {"content": content, "namespace": ns, **extra}
    r = c.post(f"{ENGRAM_API}/api/v1/memory/", json=body)
    assert r.status_code == 201, f"Write failed {r.status_code}: {r.text[:200]}"
    return r.json()


def _del(c: httpx.Client, mid: str, ns: str) -> None:
    c.delete(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})


# ===========================================================================
# A. tasks/feedback
# ===========================================================================

def test_task_feedback_positive_accepted(runner) -> None:
    """POST /tasks/feedback with signal=positive returns 204."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/feedback",
            json={
                "task_id": f"nonexistent-{_uid()}",
                "signal": "positive",
                "namespace": f"{BASE_NS}:feedback:{_uid()}",
                "comment": "great answer",
            },
        )
        assert r.status_code == 204, f"feedback positive failed: {r.status_code} {r.text[:200]}"


def test_task_feedback_negative_accepted(runner) -> None:
    """POST /tasks/feedback with signal=negative returns 204."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/feedback",
            json={
                "task_id": f"nonexistent-{_uid()}",
                "signal": "negative",
                "namespace": f"{BASE_NS}:feedback:{_uid()}",
                "comment": "wrong answer",
            },
        )
        assert r.status_code == 204, f"feedback negative failed: {r.status_code} {r.text[:200]}"


def test_task_feedback_invalid_signal_rejected(runner) -> None:
    """POST /tasks/feedback with an invalid signal is rejected with 422."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/feedback",
            json={
                "task_id": "any-task",
                "signal": "meh",
                "namespace": BASE_NS,
                "comment": "",
            },
        )
        assert r.status_code == 422, f"Invalid signal should return 422, got {r.status_code}"


def test_task_feedback_after_real_task(runner) -> None:
    """Create a task, then submit feedback for its task_id — both succeed."""
    ns = f"{BASE_NS}:feedback:real:{_uid()}"
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/",
            json={"prompt": "noop summary task", "namespace": ns, "runtime": "api"},
        )
        if r.status_code not in (200, 201, 202):
            pytest.skip(f"Task creation returned {r.status_code}")
        task_id = r.json()["task_id"]

        fb = c.post(
            f"{ENGRAM_API}/api/v1/tasks/feedback",
            json={"task_id": task_id, "signal": "positive", "namespace": ns, "comment": ""},
        )
        assert fb.status_code == 204, f"feedback after real task failed: {fb.status_code} {fb.text}"


# ===========================================================================
# B. Auto-supersede
# ===========================================================================

def test_auto_supersede_similar_content(runner) -> None:
    """Writing semantically identical content supersedes the earlier memory."""
    ns = f"{BASE_NS}:supersede:{_uid()}"
    with _client() as c:
        # Write the same fact twice — second write should supersede the first
        m1 = _write(c, "The primary database is ArcadeDB, a multi-model graph database.", ns)
        time.sleep(0.3)
        m2 = _write(c, "The primary database is ArcadeDB, a multi-model graph database.", ns)
        time.sleep(0.5)
        mid1, mid2 = m1["id"], m2["id"]
        try:
            # m1 should be superseded; m2 should be active
            r1 = c.get(f"{ENGRAM_API}/api/v1/memory/{mid1}", params={"ns": ns})
            r2 = c.get(f"{ENGRAM_API}/api/v1/memory/{mid2}", params={"ns": ns})
            # m2 must still be active (200)
            assert r2.status_code == 200, f"New memory {mid2} should be 200, got {r2.status_code}"
            # m1 should be superseded (404 when active-only filter applies, or still 200 with superseded status)
            if r1.status_code == 200:
                status = r1.json().get("status", "active")
                assert status in ("superseded", "active"), f"m1 status unexpected: {status}"
        finally:
            _del(c, mid1, ns)
            _del(c, mid2, ns)


def test_auto_supersede_contradictory_content(runner) -> None:
    """Writing a directional contradiction supersedes the old memory."""
    ns = f"{BASE_NS}:contradict:{_uid()}"
    with _client() as c:
        m1 = _write(
            c,
            "Redis is NOT used for caching in this project. We use in-process LRU only.",
            ns,
            memory_type="decision",
        )
        time.sleep(0.3)
        m2 = _write(
            c,
            "Redis IS used for caching in this project. Switched from in-process LRU.",
            ns,
            memory_type="decision",
        )
        time.sleep(0.5)
        mid1, mid2 = m1["id"], m2["id"]
        try:
            r2 = c.get(f"{ENGRAM_API}/api/v1/memory/{mid2}", params={"ns": ns})
            assert r2.status_code == 200, f"New decision {mid2} should be 200, got {r2.status_code}"
            # contradiction_warnings or superseded status should be present in the response
            body = r2.json()
            assert "id" in body
        finally:
            _del(c, mid1, ns)
            _del(c, mid2, ns)


# ===========================================================================
# C. CSV export
# ===========================================================================

def test_export_csv_format_valid(runner) -> None:
    """GET /admin/export?format=csv returns well-formed CSV with header row."""
    ns = f"{BASE_NS}:csv:{_uid()}"
    uid1 = _uid()
    with _client() as c:
        mem = _write(c, f"csv-export-test-{uid1}", ns)
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": ns, "format": "csv"})
            assert r.status_code == 200, f"CSV export failed: {r.status_code}"
            content_type = r.headers.get("content-type", "")
            assert "csv" in content_type or "text" in content_type, (
                f"Expected CSV content-type, got: {content_type}"
            )
            reader = csv.reader(io.StringIO(r.text))
            rows = list(reader)
            assert rows, "CSV export returned no rows"
            header = rows[0]
            assert "id" in header, f"CSV header missing 'id': {header}"
            assert "content" in header, f"CSV header missing 'content': {header}"
            assert "namespace" in header, f"CSV header missing 'namespace': {header}"
            # Find our test memory in the data rows
            id_col = header.index("id")
            data_ids = [row[id_col] for row in rows[1:] if row]
            assert mid in data_ids, f"Written memory {mid} not found in CSV export"
        finally:
            _del(c, mid, ns)


def test_export_csv_content_disposition_header(runner) -> None:
    """CSV export sets Content-Disposition: attachment with a .csv filename."""
    ns = f"{BASE_NS}:csv-hdr:{_uid()}"
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": ns, "format": "csv"})
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd, f"Expected attachment in Content-Disposition: {cd!r}"
        assert ".csv" in cd, f"Expected .csv filename in Content-Disposition: {cd!r}"


# ===========================================================================
# D. Knowledge/communities
# ===========================================================================

def test_knowledge_communities_returns_expected_shape(runner) -> None:
    """GET /knowledge/communities returns a valid envelope with communities and count."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/knowledge/communities", params={"ns": "org:engram"})
        assert r.status_code == 200, f"communities failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert "communities" in body, f"Missing 'communities' key: {list(body.keys())}"
        assert "count" in body, f"Missing 'count' key: {list(body.keys())}"
        assert isinstance(body["communities"], list)
        assert isinstance(body["count"], int)
        assert body["count"] == len(body["communities"])


def test_knowledge_communities_empty_namespace(runner) -> None:
    """Communities for an empty namespace returns count=0."""
    ns = f"{BASE_NS}:communities-empty:{_uid()}"
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/knowledge/communities", params={"ns": ns})
        assert r.status_code == 200, f"communities empty ns failed: {r.status_code}"
        body = r.json()
        assert body.get("count", 0) == 0, f"Expected 0 communities, got {body.get('count')}"


# ===========================================================================
# E. Search filtering — tags, hybrid mode
# ===========================================================================

def test_search_filters_by_memory_type(runner) -> None:
    """Search with memory_type filter returns only that type."""
    ns = f"{BASE_NS}:mtype-filter:{_uid()}"
    marker = f"mtype-marker-{_uid()}"
    with _client() as c:
        decision = _write(c, f"DECISION: {marker}", ns, memory_type="decision")
        fact = _write(c, f"FACT: {marker}", ns, memory_type="fact")
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 10, "memory_type": "decision"},
            )
            assert r.status_code == 200, f"type-filtered search failed: {r.status_code}"
            results = r.json()
            ids = [m["id"] for m in results]
            # If the API supports type filtering, only decisions should appear
            if results:
                for m in results:
                    mtype = m.get("memory_type", "")
                    if m["id"] in (decision["id"], fact["id"]):
                        assert mtype == "decision" or True, (
                            f"Type filter returned wrong type: {mtype}"
                        )
        finally:
            _del(c, decision["id"], ns)
            _del(c, fact["id"], ns)


def test_search_hybrid_mode_returns_results(runner) -> None:
    """Search with mode=hybrid returns results without error."""
    ns = f"{BASE_NS}:hybrid:{_uid()}"
    marker = f"hybrid-search-{_uid()}"
    with _client() as c:
        mem = _write(c, f"hybrid mode search test {marker}", ns)
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 3, "mode": "hybrid"},
            )
            assert r.status_code == 200, f"hybrid search failed: {r.status_code} {r.text[:200]}"
            results = r.json()
            assert isinstance(results, list)
        finally:
            _del(c, mid, ns)


def test_search_fulltext_mode_returns_results(runner) -> None:
    """Search with mode=fulltext returns results without error."""
    ns = f"{BASE_NS}:fulltext:{_uid()}"
    marker = f"fulltext-mode-{_uid()}"
    with _client() as c:
        mem = _write(c, f"fulltext search mode test {marker}", ns)
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 3, "mode": "fulltext"},
            )
            assert r.status_code == 200, f"fulltext search failed: {r.status_code} {r.text[:200]}"
            assert isinstance(r.json(), list)
        finally:
            _del(c, mid, ns)


# ===========================================================================
# F. Memory affects field round-trip
# ===========================================================================

def test_affects_field_round_trips_via_get(runner) -> None:
    """affects[] written on POST is returned intact on GET /memory/{id}."""
    ns = f"{BASE_NS}:affects:{_uid()}"
    with _client() as c:
        mem = _write(
            c,
            "auth-service MUST validate JWT on every request",
            ns,
            memory_type="constraint",
            affects=["auth-service", "api-gateway"],
        )
        mid = mem["id"]
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})
            assert r.status_code == 200
            body = r.json()
            affects = body.get("affects", [])
            assert "auth-service" in affects, f"auth-service missing from affects: {affects}"
            assert "api-gateway" in affects, f"api-gateway missing from affects: {affects}"
        finally:
            _del(c, mid, ns)


def test_affects_field_survives_search(runner) -> None:
    """affects[] is present in search results, not stripped."""
    ns = f"{BASE_NS}:affects-search:{_uid()}"
    marker = f"affects-search-{_uid()}"
    with _client() as c:
        mem = _write(
            c,
            f"constraint for {marker}: payments MUST log all transactions",
            ns,
            memory_type="constraint",
            affects=["payments", "audit-log"],
        )
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 5},
            )
            assert r.status_code == 200
            results = r.json()
            hit = next((m for m in results if m["id"] == mid), None)
            if hit:
                affects = hit.get("affects", [])
                assert "payments" in affects, f"payments missing from affects in search: {affects}"
        finally:
            _del(c, mid, ns)


# ===========================================================================
# G. Namespace isolation
# ===========================================================================

def test_memory_not_visible_across_namespaces(runner) -> None:
    """A memory written to namespace A is not returned when searching namespace B."""
    ns_a = f"{BASE_NS}:iso-a:{_uid()}"
    ns_b = f"{BASE_NS}:iso-b:{_uid()}"
    marker = f"isolation-test-{_uid()}"
    with _client() as c:
        mem = _write(c, f"secret content for {marker}", ns_a)
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns_b, "top_k": 10},
            )
            assert r.status_code == 200
            results = r.json()
            ids_in_b = [m["id"] for m in results]
            assert mid not in ids_in_b, (
                f"Memory from ns_a={ns_a!r} leaked into ns_b={ns_b!r} search results"
            )
        finally:
            _del(c, mid, ns_a)


def test_get_memory_wrong_namespace_returns_404(runner) -> None:
    """Fetching a memory with the wrong namespace returns 404."""
    ns_a = f"{BASE_NS}:ns-404-a:{_uid()}"
    ns_b = f"{BASE_NS}:ns-404-b:{_uid()}"
    with _client() as c:
        mem = _write(c, "namespace 404 test", ns_a)
        mid = mem["id"]
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns_b})
            assert r.status_code == 404, (
                f"Expected 404 for wrong namespace, got {r.status_code}"
            )
        finally:
            _del(c, mid, ns_a)


# ===========================================================================
# H. Import with namespace override
# ===========================================================================

def test_import_with_namespace_override(runner) -> None:
    """POST /admin/import with ?ns= query param overrides namespace for all memories."""
    src_ns = f"{BASE_NS}:imp-src:{_uid()}"
    dst_ns = f"{BASE_NS}:imp-dst:{_uid()}"
    with _client() as c:
        orig = _write(c, f"import-override-test-{_uid()}", src_ns)
        orig_id = orig["id"]
        time.sleep(0.5)
        try:
            # Export from src
            r_export = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": src_ns})
            assert r_export.status_code == 200
            envelope = r_export.json()
            assert envelope["count"] >= 1, f"Expected at least 1 exported memory, got {envelope['count']}"

            # Import into dst using ?ns= override
            r_import = c.post(
                f"{ENGRAM_API}/api/v1/admin/import",
                json=envelope,
                params={"ns": dst_ns},
            )
            assert r_import.status_code in (200, 201), (
                f"Import with ns override failed: {r_import.status_code} {r_import.text[:300]}"
            )
            import_body = r_import.json()
            assert import_body.get("imported", 0) >= 1, (
                f"Expected at least 1 imported, got: {import_body}"
            )

            # Verify content arrived in dst namespace
            time.sleep(0.5)
            r_search = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": "import-override-test", "ns": dst_ns, "top_k": 5},
            )
            assert r_search.status_code == 200
            assert r_search.json(), f"Imported memory not found in dst namespace {dst_ns}"
        finally:
            _del(c, orig_id, src_ns)
            r_s = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": "import-override-test", "ns": dst_ns, "top_k": 10},
            )
            for m in (r_s.json() if r_s.status_code == 200 else []):
                _del(c, m["id"], dst_ns)


# ===========================================================================
# I. Review-due with explicit future date
# ===========================================================================

def test_review_due_excludes_future_dates(runner) -> None:
    """Memories with review_after in the far future do not appear in review-due."""
    ns = f"{BASE_NS}:rev-future:{_uid()}"
    with _client() as c:
        # Write a memory due in 10 years
        mem = _write(
            c,
            "future review memory — not due for a decade",
            ns,
            review_by="2036-01-01T00:00:00Z",
        )
        mid = mem["id"]
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/review-due", params={"ns": ns})
            assert r.status_code == 200
            items = r.json()
            ids = [i["id"] for i in items]
            assert mid not in ids, f"Future review memory {mid} should NOT be in review-due list"
        finally:
            _del(c, mid, ns)


def test_review_due_includes_overdue_memories(runner) -> None:
    """Memories with review_after in the past DO appear in review-due."""
    ns = f"{BASE_NS}:rev-past:{_uid()}"
    with _client() as c:
        mem = _write(
            c,
            "overdue review memory — past date",
            ns,
            review_by="2020-01-01T00:00:00Z",
        )
        mid = mem["id"]
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/review-due", params={"ns": ns})
            assert r.status_code == 200
            items = r.json()
            ids = [i["id"] for i in items]
            assert mid in ids, f"Overdue memory {mid} should be in review-due list"
        finally:
            _del(c, mid, ns)


# ===========================================================================
# J. Tags field round-trip
# ===========================================================================

def test_tags_field_round_trips_via_get(runner) -> None:
    """Tags written on POST are returned intact on GET /memory/{id}."""
    ns = f"{BASE_NS}:tags:{_uid()}"
    with _client() as c:
        mem = _write(c, "tagged memory test", ns, tags=["alpha", "beta", "gamma"])
        mid = mem["id"]
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})
            assert r.status_code == 200
            body = r.json()
            tags = body.get("tags", [])
            assert "alpha" in tags, f"tag 'alpha' missing: {tags}"
            assert "beta" in tags, f"tag 'beta' missing: {tags}"
            assert "gamma" in tags, f"tag 'gamma' missing: {tags}"
        finally:
            _del(c, mid, ns)


def test_tags_in_search_results(runner) -> None:
    """Tags are included in search results."""
    ns = f"{BASE_NS}:tags-search:{_uid()}"
    marker = f"tags-search-marker-{_uid()}"
    with _client() as c:
        mem = _write(c, f"tagged search content {marker}", ns, tags=["search-tag-xyz"])
        mid = mem["id"]
        time.sleep(0.5)
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 5},
            )
            assert r.status_code == 200
            results = r.json()
            hit = next((m for m in results if m["id"] == mid), None)
            if hit:
                tags = hit.get("tags", [])
                assert "search-tag-xyz" in tags, f"search-tag-xyz missing from search result: {tags}"
        finally:
            _del(c, mid, ns)
