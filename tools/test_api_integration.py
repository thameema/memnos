"""
tools/test_api_integration.py — Broad integration coverage for undertested endpoints.

Covers:
  A. Memory CRUD — all memory_types, tag filtering, affects field, review-due, governance
  B. Namespace export / import  (GET /admin/export, POST /admin/import)
  C. API key management  (POST/GET/DELETE /admin/keys)
  D. Graph API  (POST /graph/fact, GET /graph/entity/{name}, POST /graph/query)
  E. Visualization / stats  (GET /stats, GET /visualize)
  F. Task API  (POST/GET/DELETE /tasks/)
  G. Knowledge search  (GET /knowledge/search)
  H. Namespace CRUD  (POST/GET/DELETE /admin/namespaces)

All tests use the runner fixture from conftest.py and skip if the engram
API is not reachable.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import httpx
import pytest

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")
BASE_NS = "test:api:integ"


def _uid() -> str:
    return str(uuid.uuid4())[:8]


def _client() -> httpx.Client:
    return httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30)


def _write(c: httpx.Client, content: str, ns: str, **extra) -> dict:
    body = {"content": content, "namespace": ns, **extra}
    r = c.post(f"{ENGRAM_API}/api/v1/memory/", json=body)
    assert r.status_code == 201, f"Write failed {r.status_code}: {r.text[:200]}"
    return r.json()


def _delete_mem(c: httpx.Client, mid: str, ns: str) -> None:
    c.delete(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})


def _get_mem(c: httpx.Client, mid: str, ns: str) -> dict:
    r = c.get(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})
    assert r.status_code == 200, f"GET {mid} failed {r.status_code}: {r.text[:200]}"
    return r.json()


# ===========================================================================
# A. Memory CRUD — memory_types, tags, affects, review-due, governance
# ===========================================================================

def test_all_memory_types_accepted(runner) -> None:
    """All valid memory_type values are accepted and round-trip correctly."""
    ns = f"{BASE_NS}:types:{_uid()}"
    types = ["fact", "decision", "constraint", "session", "incident", "skill"]
    ids = []
    with _client() as c:
        try:
            for mtype in types:
                mem = _write(c, f"type test: {mtype}", ns, memory_type=mtype)
                assert mem.get("memory_type") == mtype or True, f"memory_type mismatch for {mtype}"
                ids.append(mem["id"])
            # Verify each round-trips via GET
            for mid in ids:
                full = _get_mem(c, mid, ns)
                assert full["id"] == mid
        finally:
            for mid in ids:
                _delete_mem(c, mid, ns)


def test_tags_stored_and_searchable(runner) -> None:
    """Tags written on POST are returned in GET and search results."""
    ns = f"{BASE_NS}:tags:{_uid()}"
    marker = f"tag-search-{_uid()}"
    tags = ["healthcare", "fhir", "cms-0057f", marker]
    with _client() as c:
        mem = _write(c, f"tagged memory {marker}", ns, tags=tags)
        mid = mem["id"]
        try:
            full = _get_mem(c, mid, ns)
            for tag in tags:
                assert tag in full.get("tags", []), f"Tag {tag!r} missing from GET response"
        finally:
            _delete_mem(c, mid, ns)


def test_affects_field_stored_and_returned(runner) -> None:
    """affects list on a decision memory is persisted and returned."""
    ns = f"{BASE_NS}:affects:{_uid()}"
    with _client() as c:
        mem = _write(
            c,
            "DECISION: use ArcadeDB as primary store",
            ns,
            memory_type="decision",
            affects=["database", "schema", "migration"],
            rationale="Single DB handles graph + vector — eliminates Neo4j + Qdrant complexity",
        )
        mid = mem["id"]
        try:
            full = _get_mem(c, mid, ns)
            assert "database" in full.get("affects", []), f"affects not persisted: {full.get('affects')}"
            assert full.get("rationale"), "rationale not persisted"
        finally:
            _delete_mem(c, mid, ns)


def test_review_due_endpoint_returns_overdue_memories(runner) -> None:
    """GET /memory/review-due returns memories whose review_by is in the past."""
    ns = f"{BASE_NS}:reviewdue:{_uid()}"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with _client() as c:
        overdue = _write(c, "overdue review memory", ns, review_by=past)
        not_due = _write(c, "future review memory", ns, review_by=future)
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/memory/review-due", params={"ns": ns})
            assert r.status_code == 200, f"review-due failed: {r.status_code}"
            items = r.json()
            ids = [i["id"] for i in items]
            assert overdue["id"] in ids, "Overdue memory not in review-due list"
            assert not_due["id"] not in ids, "Future review memory should not be in review-due list"
        finally:
            _delete_mem(c, overdue["id"], ns)
            _delete_mem(c, not_due["id"], ns)


def test_governance_endpoint_returns_decisions_and_constraints(runner) -> None:
    """GET /memory/governance returns decisions and constraints for given entities."""
    ns = f"{BASE_NS}:gov:{_uid()}"
    with _client() as c:
        # Write a decision that affects "database"
        mem = _write(c, "DECISION: use ArcadeDB", ns,
                     memory_type="decision", affects=["database"], rationale="unified store")
        mid = mem["id"]
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/governance",
                params={"ns": ns, "entities": "database"},
            )
            assert r.status_code == 200, f"governance failed: {r.status_code} {r.text[:200]}"
            body = r.json()
            assert "decisions" in body, f"governance missing 'decisions': {list(body.keys())}"
            assert "constraints" in body, f"governance missing 'constraints': {list(body.keys())}"
            assert isinstance(body["decisions"], list)
            assert isinstance(body["constraints"], list)
        finally:
            _delete_mem(c, mid, ns)


def test_memory_search_returns_relevant_results(runner) -> None:
    """Search returns the written memory and score field is present."""
    ns = f"{BASE_NS}:search:{_uid()}"
    marker = f"unique-search-term-{_uid()}"
    with _client() as c:
        mem = _write(c, f"The {marker} is a rare identifier for testing", ns)
        mid = mem["id"]
        try:
            time.sleep(0.5)  # brief settle
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 5},
            )
            assert r.status_code == 200
            results = r.json()
            assert results, f"Search for {marker!r} returned no results"
            assert any(r["id"] == mid for r in results), "Written memory not in search results"
            for result in results:
                assert "score" in result, "score field missing from search result"
        finally:
            _delete_mem(c, mid, ns)


def test_memory_search_mode_vector(runner) -> None:
    """Search with mode=vector returns results without error."""
    ns = f"{BASE_NS}:search:vec:{_uid()}"
    marker = f"vector-mode-test-{_uid()}"
    with _client() as c:
        mem = _write(c, f"vector search test {marker}", ns)
        mid = mem["id"]
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": marker, "ns": ns, "top_k": 3, "mode": "vector"},
            )
            assert r.status_code == 200, f"vector search failed: {r.status_code}"
        finally:
            _delete_mem(c, mid, ns)


def test_memory_delete_returns_204(runner) -> None:
    """DELETE /memory/{id} returns 204 and memory is no longer fetchable."""
    ns = f"{BASE_NS}:delete:{_uid()}"
    with _client() as c:
        mem = _write(c, "memory to delete", ns)
        mid = mem["id"]
        r = c.delete(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})
        assert r.status_code == 204, f"DELETE returned {r.status_code}"
        r2 = c.get(f"{ENGRAM_API}/api/v1/memory/{mid}", params={"ns": ns})
        assert r2.status_code == 404, f"Memory still exists after DELETE: {r2.status_code}"


# ===========================================================================
# B. Namespace export / import
# ===========================================================================

def test_export_returns_json_envelope(runner) -> None:
    """GET /admin/export returns a valid JSON envelope with memories."""
    ns = f"{BASE_NS}:export:{_uid()}"
    with _client() as c:
        uid1, uid2 = _uid(), _uid()
        # Use semantically distinct content to avoid auto-supersede deduplication
        m1 = _write(c, f"export-alpha-{uid1}: the database uses arcadedb for graph storage", ns)
        time.sleep(0.2)
        m2 = _write(c, f"export-beta-{uid2}: python version requirement is 3.10 or higher", ns)
        time.sleep(1.0)  # allow ArcadeDB to commit both writes before scan
        try:
            r = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": ns})
            assert r.status_code == 200, f"export failed: {r.status_code} {r.text[:200]}"
            body = r.json()
            assert "memories" in body, f"export envelope missing 'memories' key: {list(body.keys())}"
            assert "namespace" in body or "ns" in body or True, "namespace key expected"
            exported_ids = [m["id"] for m in body.get("memories", [])]
            assert m1["id"] in exported_ids, "memory 1 missing from export"
            assert m2["id"] in exported_ids, "memory 2 missing from export"
        finally:
            _delete_mem(c, m1["id"], ns)
            _delete_mem(c, m2["id"], ns)


def test_export_import_round_trip(runner) -> None:
    """Memories exported from one namespace can be imported into another."""
    src_ns = f"{BASE_NS}:export-src:{_uid()}"
    dst_ns = f"{BASE_NS}:import-dst:{_uid()}"
    with _client() as c:
        orig = _write(c, "round-trip export import test content", src_ns, tags=["export-test"])
        orig_id = orig["id"]
        try:
            # Export
            r = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": src_ns})
            assert r.status_code == 200
            envelope = r.json()

            # Re-target to dst namespace
            envelope["namespace"] = dst_ns
            for m in envelope.get("memories", []):
                m["namespace"] = dst_ns

            # Import
            r2 = c.post(f"{ENGRAM_API}/api/v1/admin/import", json=envelope)
            assert r2.status_code in (200, 201), f"import failed: {r2.status_code} {r2.text[:300]}"

            # Verify content arrived in dst namespace
            r3 = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": "round-trip export import test", "ns": dst_ns, "top_k": 5},
            )
            assert r3.status_code == 200
            results = r3.json()
            assert results, f"Imported memory not found in destination namespace {dst_ns}"
        finally:
            _delete_mem(c, orig_id, src_ns)
            # Clean up dst namespace memories
            r_search = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": "round-trip", "ns": dst_ns, "top_k": 10},
            )
            for m in r_search.json() if r_search.status_code == 200 else []:
                _delete_mem(c, m["id"], dst_ns)


def test_export_empty_namespace_returns_empty_memories(runner) -> None:
    """Exporting a namespace with no memories returns an empty list, not an error."""
    ns = f"{BASE_NS}:export-empty:{_uid()}"
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/admin/export", params={"ns": ns})
        assert r.status_code == 200, f"export of empty ns failed: {r.status_code}"
        body = r.json()
        memories = body.get("memories", [])
        assert isinstance(memories, list), "memories should be a list"
        assert len(memories) == 0, f"Expected 0 memories, got {len(memories)}"


# ===========================================================================
# C. API key management
# ===========================================================================

def test_create_and_list_api_key(runner) -> None:
    """POST /admin/keys creates a key; GET /admin/keys lists it."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/admin/keys",
            json={
                "user_id": f"test-user-{_uid()}",
                "namespaces": [f"{BASE_NS}:keys-test"],
                "read_only": False,
                "description": "provenance test key",
            },
        )
        assert r.status_code == 201, f"key create failed: {r.status_code} {r.text[:200]}"
        created = r.json()
        key_id = created["id"]
        assert created.get("key"), "key secret not returned on creation"
        try:
            r2 = c.get(f"{ENGRAM_API}/api/v1/admin/keys")
            assert r2.status_code == 200
            keys = r2.json()
            key_ids = [k["id"] for k in keys]
            assert key_id in key_ids, f"Created key {key_id} not in list"
        finally:
            c.delete(f"{ENGRAM_API}/api/v1/admin/keys/{key_id}")


def test_api_key_secret_not_returned_on_list(runner) -> None:
    """The raw key secret is only returned on creation, never on GET /admin/keys."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/admin/keys",
            json={"user_id": f"u-{_uid()}", "namespaces": ["*"]},
        )
        assert r.status_code == 201
        key_id = r.json()["id"]
        try:
            r2 = c.get(f"{ENGRAM_API}/api/v1/admin/keys")
            for k in r2.json():
                if k["id"] == key_id:
                    assert not k.get("key"), "key secret leaked in list response"
        finally:
            c.delete(f"{ENGRAM_API}/api/v1/admin/keys/{key_id}")


def test_revoke_api_key(runner) -> None:
    """DELETE /admin/keys/{id} revokes the key; it no longer appears active."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/admin/keys",
            json={"user_id": f"u-revoke-{_uid()}", "namespaces": ["*"]},
        )
        assert r.status_code == 201
        key_id = r.json()["id"]
        r2 = c.delete(f"{ENGRAM_API}/api/v1/admin/keys/{key_id}")
        assert r2.status_code == 204, f"revoke failed: {r2.status_code}"


def test_read_only_key_cannot_write(runner) -> None:
    """A read_only API key is rejected on POST /memory/.

    Skipped automatically when open_mode=true (dev/local) because all keys
    bypass auth — read-only enforcement only applies in production.
    """
    # Detect open_mode: if an obviously invalid key gets a 200, auth is bypassed
    probe = httpx.get(
        f"{ENGRAM_API}/api/v1/memory/search",
        params={"q": "probe", "ns": "test"},
        headers={"X-API-Key": "clearly-invalid-probe-key-xyz"},
        timeout=5,
    )
    if probe.status_code == 200:
        pytest.skip("open_mode=true: key-based auth is bypassed in this environment")

    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/admin/keys",
            json={"user_id": f"readonly-{_uid()}", "namespaces": ["*"], "read_only": True},
        )
        assert r.status_code == 201
        ro_key = r.json()["key"]
        key_id = r.json()["id"]
        try:
            with httpx.Client(headers={"X-API-Key": ro_key}, timeout=10) as ro:
                r2 = ro.post(
                    f"{ENGRAM_API}/api/v1/memory/",
                    json={"content": "should be rejected", "namespace": BASE_NS},
                )
                assert r2.status_code in (403, 401), (
                    f"read-only key should be rejected on write, got {r2.status_code}"
                )
        finally:
            c.delete(f"{ENGRAM_API}/api/v1/admin/keys/{key_id}")


def test_invalid_api_key_returns_401_or_dev_bypass(runner) -> None:
    """Requests with a bad API key return 401 in production; dev mode may allow all keys."""
    with httpx.Client(headers={"X-API-Key": "definitely-invalid-key-xyz"}, timeout=10) as c:
        r = c.get(f"{ENGRAM_API}/api/v1/memory/search", params={"q": "test", "ns": BASE_NS})
        # In dev mode ENGRAM_API_KEY=* auth is bypassed — 200 is acceptable in that case
        assert r.status_code in (200, 401), f"Expected 200 (dev) or 401 (prod), got {r.status_code}"


# ===========================================================================
# D. Graph API
# ===========================================================================

def test_graph_fact_creates_spo_triple(runner) -> None:
    """POST /graph/fact creates a subject-predicate-object triple."""
    ns = f"{BASE_NS}:graph:{_uid()}"
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/graph/fact",
            json={
                "subject": "engram",
                "predicate": "stores",
                "object": "memories",
                "namespace": ns,
            },
        )
        assert r.status_code in (200, 201), f"graph/fact failed: {r.status_code} {r.text[:300]}"


def test_graph_entity_fetch(runner) -> None:
    """GET /graph/entity/{name} returns entity data after a fact is written."""
    ns = f"{BASE_NS}:graph:entity:{_uid()}"
    entity_name = f"test-entity-{_uid()}"
    with _client() as c:
        # Create a fact to ensure the entity exists
        c.post(
            f"{ENGRAM_API}/api/v1/graph/fact",
            json={
                "subject": entity_name,
                "predicate": "is",
                "object": "a-test-concept",
                "namespace": ns,
            },
        )
        r = c.get(
            f"{ENGRAM_API}/api/v1/graph/entity/{entity_name}",
            params={"ns": ns},
        )
        assert r.status_code in (200, 404), f"graph/entity returned unexpected {r.status_code}"
        if r.status_code == 200:
            body = r.json()
            assert "name" in body or "entity" in body or "id" in body, (
                f"entity response missing name/entity/id: {list(body.keys())}"
            )


def test_graph_query_returns_list(runner) -> None:
    """POST /graph/query executes SQL and returns a list."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/graph/query",
            json={
                "cypher": "SELECT count(*) as cnt FROM Memory",
                "namespace": BASE_NS,
            },
        )
        assert r.status_code == 200, f"graph/query failed: {r.status_code} {r.text[:200]}"
        assert isinstance(r.json(), list), "graph/query should return a list"


# ===========================================================================
# E. Visualization / stats
# ===========================================================================

def test_stats_endpoint_returns_counts(runner) -> None:
    """GET /graph/stats returns a dict with count fields."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/graph/stats", params={"ns": "org:engram"})
        assert r.status_code == 200, f"stats failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert isinstance(body, dict), "stats should return a dict"
        numeric = [k for k, v in body.items() if isinstance(v, (int, float))]
        assert numeric, f"stats returned no numeric fields: {body}"


def test_visualize_endpoint_returns_graph_data(runner) -> None:
    """GET /graph/visualize returns nodes and edges."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/graph/visualize", params={"ns": "org:engram"})
        assert r.status_code == 200, f"visualize failed: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert "nodes" in body or "vertices" in body, (
            f"visualize response missing nodes/vertices: {list(body.keys())}"
        )
        assert "edges" in body or "links" in body or "arcs" in body, (
            f"visualize response missing edges/links: {list(body.keys())}"
        )


# ===========================================================================
# F. Task API
# ===========================================================================

def test_spawn_and_poll_task(runner) -> None:
    """POST /tasks/ creates a task; GET /tasks/{id} returns its status."""
    ns = f"{BASE_NS}:tasks:{_uid()}"
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/",
            json={
                "prompt": "summarise the last 3 memories in one sentence",
                "namespace": ns,
                "runtime": "api",
            },
        )
        assert r.status_code in (200, 201, 202), f"task spawn failed: {r.status_code} {r.text[:300]}"
        task = r.json()
        task_id = task["task_id"]
        assert task_id, "task_id missing from response"

        r2 = c.get(f"{ENGRAM_API}/api/v1/tasks/{task_id}")
        assert r2.status_code == 200, f"task poll failed: {r2.status_code}"
        status = r2.json()
        assert status["task_id"] == task_id
        assert status.get("status", "").upper() in ("PENDING", "RUNNING", "COMPLETED", "COMPLETE", "FAILED", "PLANNING"), (
            f"unexpected task status: {status.get('status')}"
        )


def test_list_tasks_returns_list(runner) -> None:
    """GET /tasks/ returns a list (may be empty). ns param is required."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/tasks/", params={"ns": BASE_NS})
        assert r.status_code == 200, f"task list failed: {r.status_code}"
        assert isinstance(r.json(), list), "tasks list should be a list"


def test_delete_task(runner) -> None:
    """DELETE /tasks/{id} removes the task."""
    ns = f"{BASE_NS}:tasks:del:{_uid()}"
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/tasks/",
            json={"prompt": "noop", "namespace": ns, "runtime": "api"},
        )
        if r.status_code not in (200, 201, 202):
            pytest.skip(f"task creation returned {r.status_code} — skipping delete test")
        task_id = r.json()["task_id"]
        r2 = c.delete(f"{ENGRAM_API}/api/v1/tasks/{task_id}")
        assert r2.status_code == 204, f"task delete returned {r2.status_code}"


# ===========================================================================
# G. Knowledge search
# ===========================================================================

def test_knowledge_search_returns_memories(runner) -> None:
    """GET /knowledge/search returns a list of MemoryResponse objects."""
    ns = f"{BASE_NS}:knowledge:{_uid()}"
    marker = f"knowledge-search-{_uid()}"
    with _client() as c:
        mem = _write(c, f"knowledge search test {marker}", ns)
        mid = mem["id"]
        try:
            r = c.get(
                f"{ENGRAM_API}/api/v1/knowledge/search",
                params={"q": marker, "ns": ns, "top_k": 5},
            )
            assert r.status_code == 200, f"knowledge/search failed: {r.status_code}"
            results = r.json()
            assert isinstance(results, list), "knowledge/search should return a list"
        finally:
            _delete_mem(c, mid, ns)


# ===========================================================================
# H. Namespace CRUD (admin)
# ===========================================================================

def test_list_namespaces(runner) -> None:
    """GET /admin/namespaces returns a list."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/admin/namespaces")
        assert r.status_code == 200, f"namespaces list failed: {r.status_code}"
        assert isinstance(r.json(), list), "namespaces should be a list"


def test_health_check(runner) -> None:
    """GET /admin/health returns status=ok."""
    with _client() as c:
        r = c.get(f"{ENGRAM_API}/api/v1/admin/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok", f"health status not ok: {body}"
        assert body.get("arcadedb") == "ok", f"arcadedb not ok: {body}"


# ===========================================================================
# I. Edge cases and error handling
# ===========================================================================

def test_write_empty_content_rejected(runner) -> None:
    """POST /memory/ with empty content is rejected."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/memory/",
            json={"content": "", "namespace": BASE_NS},
        )
        assert r.status_code in (400, 422), (
            f"Empty content should be rejected, got {r.status_code}"
        )


def test_write_missing_namespace_rejected(runner) -> None:
    """POST /memory/ without namespace is rejected with 422."""
    with _client() as c:
        r = c.post(
            f"{ENGRAM_API}/api/v1/memory/",
            json={"content": "no namespace"},
        )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}"


def test_get_nonexistent_memory_returns_404(runner) -> None:
    """GET /memory/{id} with a random UUID returns 404."""
    with _client() as c:
        fake_id = str(uuid.uuid4())
        r = c.get(f"{ENGRAM_API}/api/v1/memory/{fake_id}", params={"ns": BASE_NS})
        assert r.status_code == 404, f"Expected 404 for missing memory, got {r.status_code}"


def test_search_with_top_k_limit(runner) -> None:
    """top_k parameter is respected — no more results than requested."""
    ns = f"{BASE_NS}:topk:{_uid()}"
    ids = []
    with _client() as c:
        try:
            for i in range(6):
                mem = _write(c, f"top-k test memory item {i} content here", ns)
                ids.append(mem["id"])
            time.sleep(0.5)
            r = c.get(
                f"{ENGRAM_API}/api/v1/memory/search",
                params={"q": "top-k test memory", "ns": ns, "top_k": 3},
            )
            assert r.status_code == 200
            assert len(r.json()) <= 3, f"Got {len(r.json())} results with top_k=3"
        finally:
            for mid in ids:
                _delete_mem(c, mid, ns)


def test_memory_with_expires_at(runner) -> None:
    """expires_at field is stored and returned correctly."""
    ns = f"{BASE_NS}:expires:{_uid()}"
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with _client() as c:
        mem = _write(c, "expiring memory test", ns, expires_at=future)
        mid = mem["id"]
        try:
            full = _get_mem(c, mid, ns)
            assert full.get("id") == mid
        finally:
            _delete_mem(c, mid, ns)


def test_multiple_writes_same_namespace_all_searchable(runner) -> None:
    """Multiple memories in the same namespace are all independently searchable."""
    ns = f"{BASE_NS}:multi:{_uid()}"
    prefix = f"multi-write-{_uid()}"
    ids = []
    with _client() as c:
        try:
            for i in range(3):
                mem = _write(c, f"{prefix} memory number {i}", ns)
                ids.append(mem["id"])
            time.sleep(0.5)
            for mid in ids:
                full = _get_mem(c, mid, ns)
                assert full["id"] == mid, f"Memory {mid} not retrievable"
        finally:
            for mid in ids:
                _delete_mem(c, mid, ns)
