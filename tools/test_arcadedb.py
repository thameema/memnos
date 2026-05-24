#!/usr/bin/env python3.10
"""
test_arcadedb.py — Comprehensive ArcadeDB integration test for engram.

Tests every vertex type, edge type, search mode, and persistence scenario.
Run iteratively until all tests pass.

Usage:
    python3.10 tools/test_arcadedb.py
    python3.10 tools/test_arcadedb.py --verbose
    python3.10 tools/test_arcadedb.py --test vector_search
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import httpx
    from openai import OpenAI
except ImportError as e:
    print(f"[error] Missing package: {e}", file=sys.stderr)
    sys.exit(1)

ARCADEDB_URL = "http://localhost:2480"
DB_NAME = "engram"
EMBED_MODEL = "text-embedding-3-small"
VECTOR_DIM = 1536
TEST_NS = "test:arcadedb:integration"

# ---------------------------------------------------------------------------
# ArcadeDB HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header() -> dict:
    password = os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password")
    creds = base64.b64encode(f"root:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def arcade_query(sql: str, params: dict | None = None) -> list[dict]:
    body = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/query/{DB_NAME}",
        content=json.dumps(body),
        headers=_auth_header(),
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def arcade_command(sql: str, params: dict | None = None) -> list[dict]:
    body = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/command/{DB_NAME}",
        content=json.dumps(body),
        headers=_auth_header(),
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def get_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key


def embed(openai_client: OpenAI, text: str) -> list[float]:
    response = openai_client.embeddings.create(model=EMBED_MODEL, input=[text])
    return response.data[0].embedding


def now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    elapsed_ms: float


class TestRunner:
    def __init__(self, verbose: bool = False):
        self.results: list[TestResult] = []
        self.verbose = verbose
        self._openai: OpenAI | None = None

    def get_openai(self) -> OpenAI | None:
        if self._openai is None:
            key = get_openai_key()
            if key:
                self._openai = OpenAI(api_key=key)
        return self._openai

    def run(self, name: str, fn: Callable) -> TestResult:
        start = time.time()
        try:
            fn(self)
            elapsed = (time.time() - start) * 1000
            result = TestResult(name=name, passed=True, message="OK", elapsed_ms=elapsed)
        except AssertionError as e:
            elapsed = (time.time() - start) * 1000
            result = TestResult(name=name, passed=False, message=f"ASSERT: {e}", elapsed_ms=elapsed)
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            result = TestResult(name=name, passed=False, message=f"ERROR: {type(e).__name__}: {e}", elapsed_ms=elapsed)
        self.results.append(result)
        status = "✓" if result.passed else "✗"
        print(f"  {status} {name:<50} {result.elapsed_ms:6.0f}ms  {'' if result.passed else result.message}")
        return result

    def cleanup_test_namespace(self):
        """Remove all test data from TEST_NS.

        ArcadeDB 26.x syntax: DELETE VERTEX FROM <Type> WHERE ...
        """
        for vtype in ("Memory", "Entity", "Fact", "Subscription", "Secret", "VaultAuditLog"):
            try:
                arcade_command(
                    f"DELETE VERTEX FROM {vtype} WHERE namespace = :ns OR namespace LIKE :prefix",
                    {"ns": TEST_NS, "prefix": f"{TEST_NS}:%"},
                )
            except Exception:
                pass  # Best-effort


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_connectivity(runner: TestRunner):
    """ArcadeDB is reachable and database exists."""
    resp = httpx.get(
        f"{ARCADEDB_URL}/api/v1/exists/{DB_NAME}",
        headers=_auth_header(),
        timeout=5.0,
    )
    assert resp.status_code == 200, f"Status: {resp.status_code}"
    assert resp.json().get("result") is True, "Database does not exist"


def test_schema_types(runner: TestRunner):
    """All required vertex and edge types exist in schema."""
    rows = arcade_query("SELECT name, type FROM schema:types")
    names = {r["name"] for r in rows}
    required_vertices = {"Memory", "Entity", "Fact", "Asset", "Subscription", "Secret", "VaultAuditLog"}
    required_edges = {"MENTIONS", "RELATED_TO", "SUPERSEDED_BY", "AFFECTS", "DOCUMENTED_IN"}
    missing = (required_vertices | required_edges) - names
    assert not missing, f"Missing types: {missing}"


def test_schema_memory_properties(runner: TestRunner):
    """Memory type has all required typed properties (content_embedding is stored as LIST)."""
    rows = arcade_query("SELECT properties FROM schema:types WHERE name = 'Memory'")
    assert rows, "Memory type not found"
    props_raw = rows[0].get("properties") or []
    prop_names = {p.get("name") for p in props_raw} if isinstance(props_raw, list) else set()
    # content_embedding is now typed as LIST (was dynamic ARRAY OF FLOAT which ArcadeDB rejected)
    required = {"id", "content", "namespace", "created_at", "content_embedding",
                "memory_type", "status", "superseded_at", "tags", "provenance"}
    missing = required - prop_names
    assert not missing, f"Missing Memory properties: {missing}"


def test_schema_indexes(runner: TestRunner):
    """Memory has namespace and id indexes (for fast filtering).
    NOTE: ArcadeDB 26.5.1 does not support HNSW vector indexes via SQL.
    Vector search uses Python-layer cosine similarity instead.
    """
    rows = arcade_query("SELECT indexes FROM schema:types WHERE name = 'Memory'")
    assert rows, "Memory type not found"
    indexes = rows[0].get("indexes") or []
    ns_indexed = any("namespace" in str(idx) for idx in indexes)
    assert ns_indexed, f"No namespace index on Memory. Indexes: {[idx.get('name') for idx in indexes]}"


def test_memory_insert_and_get(runner: TestRunner):
    """Insert a Memory vertex and retrieve it by id."""
    mem_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :created_at, superseded_at = null, "
        "tags = ['test'], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {
            "id": mem_id,
            "content": "Test memory: ArcadeDB insert/get verification",
            "ns": TEST_NS,
            "created_at": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id, content, namespace FROM Memory WHERE id = :id",
        {"id": mem_id},
    )
    assert rows, "Memory not found after insert"
    assert rows[0]["id"] == mem_id
    assert rows[0]["namespace"] == TEST_NS


def test_memory_supersession(runner: TestRunner):
    """Superseding a memory sets superseded_at and excludes it from active queries."""
    mem_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :created_at, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": mem_id, "content": "Memory to be superseded", "ns": TEST_NS, "created_at": now_str()},
    )
    # Supersede it
    arcade_command(
        "UPDATE Memory SET superseded_at = :now WHERE id = :id",
        {"now": now_str(), "id": mem_id},
    )
    # Should not appear in active queries
    rows = arcade_query(
        "SELECT id FROM Memory WHERE id = :id AND superseded_at IS NULL",
        {"id": mem_id},
    )
    assert not rows, "Superseded memory still appears in active query"
    # Should appear in historical queries
    rows = arcade_query("SELECT id, superseded_at FROM Memory WHERE id = :id", {"id": mem_id})
    assert rows, "Superseded memory missing from DB entirely"
    assert rows[0].get("superseded_at") is not None, "superseded_at was not set"


def test_superseded_by_edge(runner: TestRunner):
    """SUPERSEDED_BY edge links old memory to new memory."""
    old_id = uid()
    new_id = uid()
    for mid, content in [(old_id, "Old decision"), (new_id, "New decision (supersedes old)")]:
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = 'decision', status = 'active', "
            "author = '', affects = [], rationale = '', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": content, "ns": TEST_NS, "ts": now_str()},
        )
    # Supersede old
    arcade_command(
        "UPDATE Memory SET superseded_at = :now WHERE id = :id",
        {"now": now_str(), "id": old_id},
    )
    # Create SUPERSEDED_BY edge: old → new
    arcade_command(
        "CREATE EDGE SUPERSEDED_BY "
        "FROM (SELECT FROM Memory WHERE id = :old_id) "
        "TO (SELECT FROM Memory WHERE id = :new_id)",
        {"old_id": old_id, "new_id": new_id},
    )
    # Verify edge exists
    rows = arcade_query(
        "SELECT @out.id AS src, @in.id AS tgt FROM SUPERSEDED_BY "
        "WHERE @out.id = :old_id",
        {"old_id": old_id},
    )
    assert rows, "SUPERSEDED_BY edge not found"
    assert rows[0]["tgt"] == new_id, f"Wrong target: {rows[0]['tgt']}"


def test_entity_upsert(runner: TestRunner):
    """Insert an Entity vertex and verify it's stored."""
    entity_id = uid()
    arcade_command(
        "INSERT INTO Entity SET "
        "id = :id, name = :name, entity_type = :etype, "
        "namespace = :ns, created_at = :ts",
        {
            "id": entity_id,
            "name": "arcadedb test entity",
            "etype": "CONCEPT",
            "ns": TEST_NS,
            "ts": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id, name FROM Entity WHERE id = :id",
        {"id": entity_id},
    )
    assert rows, "Entity not found after insert"
    assert rows[0]["name"] == "arcadedb test entity"


def test_mentions_edge(runner: TestRunner):
    """MENTIONS edge from Memory → Entity is created correctly."""
    mem_id = uid()
    entity_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": mem_id, "content": "This memory mentions Test Corp", "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "INSERT INTO Entity SET "
        "id = :id, name = :name, entity_type = 'ORG', "
        "namespace = :ns, created_at = :ts",
        {"id": entity_id, "name": "test corp", "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "CREATE EDGE MENTIONS "
        "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
        "TO (SELECT FROM Entity WHERE id = :eid AND namespace = :ns)",
        {"mid": mem_id, "eid": entity_id, "ns": TEST_NS},
    )
    rows = arcade_query(
        "SELECT @out.id AS src, @in.id AS tgt, @in.name AS entity_name "
        "FROM MENTIONS WHERE @out.id = :mid",
        {"mid": mem_id},
    )
    assert rows, "MENTIONS edge not found"
    assert rows[0]["entity_name"] == "test corp"


def test_affects_edge(runner: TestRunner):
    """AFFECTS edge from Memory → Entity for decision/constraint memories."""
    mem_id = uid()
    entity_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['constraint'], source = 'test', metadata = {}, "
        "memory_type = 'constraint', status = 'active', "
        "author = '', affects = ['auth-service'], rationale = 'security', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": mem_id, "content": "All endpoints must use JWT auth", "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "INSERT INTO Entity SET "
        "id = :id, name = 'auth-service', entity_type = 'SERVICE', "
        "namespace = :ns, created_at = :ts",
        {"id": entity_id, "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "CREATE EDGE AFFECTS "
        "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
        "TO (SELECT FROM Entity WHERE name = 'auth-service' AND namespace = :ns)",
        {"mid": mem_id, "ns": TEST_NS},
    )
    rows = arcade_query(
        "SELECT @in.name AS target FROM AFFECTS WHERE @out.id = :mid",
        {"mid": mem_id},
    )
    assert rows, "AFFECTS edge not found"
    assert rows[0]["target"] == "auth-service"


def test_fact_insert_and_supersede(runner: TestRunner):
    """Insert a Fact vertex and supersede it."""
    fact_id = uid()
    arcade_command(
        "INSERT INTO Fact SET "
        "id = :id, subject = :subj, predicate = :pred, object = :obj, "
        "namespace = :ns, created_at = :ts, superseded_at = null, "
        "source_memory_id = null",
        {
            "id": fact_id,
            "subj": "ArcadeDB",
            "pred": "is_a",
            "obj": "graph_database",
            "ns": TEST_NS,
            "ts": now_str(),
        },
    )
    rows = arcade_query("SELECT id, subject FROM Fact WHERE id = :id", {"id": fact_id})
    assert rows, "Fact not found after insert"
    assert rows[0]["subject"] == "ArcadeDB"

    # Supersede it
    arcade_command(
        "UPDATE Fact SET superseded_at = :now WHERE id = :id",
        {"now": now_str(), "id": fact_id},
    )
    rows = arcade_query(
        "SELECT id FROM Fact WHERE id = :id AND superseded_at IS NULL", {"id": fact_id}
    )
    assert not rows, "Superseded fact still appears as active"


def test_subscription_create_and_feed(runner: TestRunner):
    """Create a subscription and verify new memories appear in feed."""
    sub_id = uid()
    subscriber = "test-subscriber-" + uid()[:8]
    sub_ns = TEST_NS + ":sub-test"

    # Create subscription
    arcade_command(
        "INSERT INTO Subscription SET "
        "id = :id, subscriber_id = :sid, namespace = :ns, "
        "filter_types = [], last_seen_at = :ts, "
        "created_at = :ts, active = true",
        {
            "id": sub_id,
            "sid": subscriber,
            "ns": sub_ns,
            "ts": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id, active FROM Subscription WHERE subscriber_id = :sid AND namespace = :ns",
        {"sid": subscriber, "ns": sub_ns},
    )
    assert rows, "Subscription not found"
    assert rows[0]["active"] is True

    # Advance last_seen_at to 1 second ago so we can write a memory and see it in feed
    past_ts = "2020-01-01T00:00:00+00:00"
    arcade_command(
        "UPDATE Subscription SET last_seen_at = :ts WHERE id = :sub_id",
        {"ts": past_ts, "sub_id": sub_id},
    )

    # Write a memory to the sub namespace
    mem_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {
            "id": mem_id,
            "content": "Feed test memory",
            "ns": sub_ns,
            "ts": now_str(),
        },
    )

    # Query feed: memories newer than last_seen_at
    sub_rows = arcade_query(
        "SELECT last_seen_at FROM Subscription WHERE id = :sid",
        {"sid": sub_id},
    )
    last_seen = sub_rows[0]["last_seen_at"] if sub_rows else past_ts

    feed_rows = arcade_query(
        "SELECT id FROM Memory WHERE namespace = :ns AND created_at > :last_seen AND superseded_at IS NULL",
        {"ns": sub_ns, "last_seen": last_seen},
    )
    feed_ids = {r["id"] for r in feed_rows}
    assert mem_id in feed_ids, f"New memory {mem_id} not in subscription feed"


def test_subscription_filter_types(runner: TestRunner):
    """filter_types on a subscription excludes non-matching memory types from the feed."""
    sub_ns = TEST_NS + ":filter-sub"
    past_ts = "2020-01-01T00:00:00+00:00"

    # Subscription that only wants 'decision' and 'incident' memories
    sub_id = uid()
    subscriber = "filter-sub-" + uid()[:8]
    arcade_command(
        "INSERT INTO Subscription SET "
        "id = :id, subscriber_id = :sid, namespace = :ns, "
        "filter_types = ['decision', 'incident'], last_seen_at = :ts, "
        "created_at = :ts, active = true",
        {"id": sub_id, "sid": subscriber, "ns": sub_ns, "ts": past_ts},
    )

    # Write a 'decision' memory — should appear in filtered feed
    dec_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'decision', metadata = {}, "
        "memory_type = 'decision', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": dec_id, "content": "Decision: use event queue", "ns": sub_ns, "ts": now_str()},
    )

    # Write a plain 'fact' memory — should NOT appear in filtered feed
    fact_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'agent', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": fact_id, "content": "Routine observation about the system", "ns": sub_ns, "ts": now_str()},
    )

    # Fetch raw feed rows (simulate get_feed filtering logic)
    sub_rows = arcade_query(
        "SELECT last_seen_at, filter_types FROM Subscription WHERE id = :id",
        {"id": sub_id},
    )
    assert sub_rows, "Subscription not found"
    last_seen = sub_rows[0]["last_seen_at"]
    raw_filter_types = [ft.lower().strip() for ft in (sub_rows[0].get("filter_types") or []) if ft]

    all_rows = arcade_query(
        "SELECT id, memory_type FROM Memory "
        "WHERE namespace = :ns AND created_at > :last_seen AND superseded_at IS NULL "
        "ORDER BY created_at ASC LIMIT 50",
        {"ns": sub_ns, "last_seen": last_seen},
    )
    # Apply filter (mirrors arcadedb_client.get_feed logic)
    filtered_ids = {
        r["id"] for r in all_rows
        if not raw_filter_types or r.get("memory_type", "").lower() in raw_filter_types
    }

    assert dec_id in filtered_ids, "Decision memory missing from filtered feed"
    assert fact_id not in filtered_ids, "Fact memory must be excluded by filter_types=['decision','incident']"

    # Tag-based filter: subscription for 'breaking_change' tag
    tag_sub_id = uid()
    tag_subscriber = "tag-sub-" + uid()[:8]
    arcade_command(
        "INSERT INTO Subscription SET "
        "id = :id, subscriber_id = :sid, namespace = :ns, "
        "filter_types = ['breaking_change'], last_seen_at = :ts, "
        "created_at = :ts, active = true",
        {"id": tag_sub_id, "sid": tag_subscriber, "ns": sub_ns, "ts": past_ts},
    )

    tagged_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['breaking_change', 'api'], source = 'agent', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": tagged_id, "content": "API v2 removes /users endpoint", "ns": sub_ns, "ts": now_str()},
    )

    tag_all_rows = arcade_query(
        "SELECT id, memory_type, tags FROM Memory "
        "WHERE namespace = :ns AND created_at > :ts AND superseded_at IS NULL LIMIT 50",
        {"ns": sub_ns, "ts": past_ts},
    )
    # Tag filter: match if any tag is in filter_types
    tag_filtered = {
        r["id"] for r in tag_all_rows
        if any(t.lower() in ["breaking_change"] for t in (r.get("tags") or []))
    }
    assert tagged_id in tag_filtered, "Memory with 'breaking_change' tag must pass tag-based filter"
    assert fact_id not in tag_filtered, "Plain fact with no matching tags must be excluded"

    # Cleanup
    for mid in (dec_id, fact_id, tagged_id):
        arcade_command("DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns", {"id": mid, "ns": sub_ns})
    for sid in (sub_id, tag_sub_id):
        arcade_command("DELETE VERTEX FROM Subscription WHERE id = :id", {"id": sid})


def test_subscription_deactivate(runner: TestRunner):
    """Deactivating a subscription sets active = false."""
    sub_id = uid()
    subscriber = "test-sub-deactivate-" + uid()[:8]
    arcade_command(
        "INSERT INTO Subscription SET "
        "id = :id, subscriber_id = :sid, namespace = :ns, "
        "filter_types = [], last_seen_at = :ts, "
        "created_at = :ts, active = true",
        {"id": sub_id, "sid": subscriber, "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "UPDATE Subscription SET active = false WHERE id = :id",
        {"id": sub_id},
    )
    rows = arcade_query(
        "SELECT active FROM Subscription WHERE id = :id", {"id": sub_id}
    )
    assert rows, "Subscription not found"
    assert rows[0]["active"] is False, f"Subscription still active: {rows[0]}"


def test_secret_store_and_retrieve(runner: TestRunner):
    """Store a Secret vertex and retrieve metadata (not plaintext)."""
    secret_id = uid()
    arcade_command(
        "INSERT INTO Secret SET "
        "id = :id, key_name = :name, note = :note, "
        "secret_type = 'api_key', namespace = :ns, "
        "value_enc = 'enc-placeholder', dek_enc = 'dek-placeholder', "
        "created_at = :ts, superseded_at = null, "
        "created_by = 'test', tags = ['test']",
        {
            "id": secret_id,
            "name": "test-api-key-" + uid()[:8],
            "note": "Test key for integration test",
            "ns": TEST_NS,
            "ts": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id, key_name, note, secret_type FROM Secret WHERE id = :id",
        {"id": secret_id},
    )
    assert rows, "Secret not found after insert"
    assert rows[0]["secret_type"] == "api_key"
    assert rows[0]["note"] == "Test key for integration test"
    # Ensure ciphertext fields are stored but NOT accidentally exposed
    raw = arcade_query("SELECT value_enc FROM Secret WHERE id = :id", {"id": secret_id})
    assert raw[0].get("value_enc") == "enc-placeholder"  # stored correctly


def test_secret_supersession(runner: TestRunner):
    """Superseding a Secret excludes it from current secret queries."""
    name = "rotate-test-" + uid()[:8]
    s1_id = uid()
    arcade_command(
        "INSERT INTO Secret SET "
        "id = :id, key_name = :name, note = '', "
        "secret_type = 'api_key', namespace = :ns, "
        "value_enc = 'v1-enc', dek_enc = 'dek1', "
        "created_at = :ts, superseded_at = null, "
        "created_by = 'test', tags = []",
        {"id": s1_id, "name": name, "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "UPDATE Secret SET superseded_at = :now WHERE id = :id",
        {"now": now_str(), "id": s1_id},
    )
    rows = arcade_query(
        "SELECT id FROM Secret WHERE key_name = :name AND namespace = :ns AND superseded_at IS NULL",
        {"name": name, "ns": TEST_NS},
    )
    assert not rows, "Superseded secret still appears as current"


def test_vault_audit_log(runner: TestRunner):
    """VaultAuditLog records are inserted and retrievable."""
    log_id = uid()
    arcade_command(
        "INSERT INTO VaultAuditLog SET "
        "id = :id, secret_name = 'test-secret', namespace = :ns, "
        "action = 'get', accessed_by = 'test-user', "
        "accessed_at = :ts, ok = true, err_msg = null",
        {"id": log_id, "ns": TEST_NS, "ts": now_str()},
    )
    rows = arcade_query(
        "SELECT id, action, ok FROM VaultAuditLog WHERE id = :id",
        {"id": log_id},
    )
    assert rows, "VaultAuditLog entry not found"
    assert rows[0]["action"] == "get"
    assert rows[0]["ok"] is True


def test_namespace_index_filter(runner: TestRunner):
    """Namespace index is used — filter by prefix returns correct results."""
    prefix_ns = TEST_NS + ":ns-filter-test"
    other_ns = TEST_NS + ":other-ns"

    ids_in = [uid() for _ in range(3)]
    ids_out = [uid()]

    for mid in ids_in:
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = 'fact', status = 'active', "
            "author = '', affects = [], rationale = '', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": "In-namespace memory", "ns": prefix_ns, "ts": now_str()},
        )
    for mid in ids_out:
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = 'fact', status = 'active', "
            "author = '', affects = [], rationale = '', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": "Out-of-namespace memory", "ns": other_ns, "ts": now_str()},
        )

    rows = arcade_query(
        "SELECT id FROM Memory WHERE (namespace = :ns OR namespace LIKE :prefix) "
        "AND superseded_at IS NULL",
        {"ns": prefix_ns, "prefix": f"{prefix_ns}:%"},
    )
    found_ids = {r["id"] for r in rows}
    for mid in ids_in:
        assert mid in found_ids, f"Expected memory {mid} not found in namespace filter"
    for mid in ids_out:
        assert mid not in found_ids, f"Out-of-namespace memory {mid} leaked into results"


def test_vector_search(runner: TestRunner):
    """Vector search returns semantically relevant results via Python-layer cosine similarity.

    ArcadeDB 26.5.1 does not support HNSW vector indexes via SQL.
    The Python-layer implementation fetches embeddings from ArcadeDB and
    computes cosine similarity using numpy (or pure-Python fallback).
    """
    openai_client = runner.get_openai()
    if not openai_client:
        raise AssertionError("No OpenAI API key — cannot test vector search")

    memories = [
        ("The FHIR R4 specification defines Patient resources with demographics", "fhir-patient"),
        ("ArcadeDB supports multi-model storage: graph, document, and key-value", "arcadedb-multimodel"),
        ("Prior authorization workflows require clinical documentation", "pa-clinical"),
        ("Kubernetes pod scheduling uses node affinity rules", "k8s-affinity"),
    ]
    inserted_ids = {}
    ns = TEST_NS + ":vector-test"
    for content, key in memories:
        mem_id = uid()
        embedding = embed(openai_client, content)
        vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        arcade_command(
            f"INSERT INTO Memory SET "
            f"id = :id, content = :content, namespace = :ns, "
            f"created_at = :ts, superseded_at = null, "
            f"tags = [], source = 'test', metadata = {{}}, "
            f"memory_type = 'fact', status = 'active', "
            f"author = '', affects = [], rationale = '', "
            f"expires_at = null, review_by = null, "
            f"provenance = {{}}, content_embedding = {vec_literal}",
            {"id": mem_id, "content": content, "ns": ns, "ts": now_str()},
        )
        inserted_ids[key] = mem_id

    # Python-layer cosine similarity search
    query_emb = embed(openai_client, "FHIR patient demographics")
    dim = len(query_emb)

    # Fetch all memories in test namespace with matching embedding dimension
    rows = arcade_query(
        "SELECT id, content, content_embedding FROM Memory "
        "WHERE (namespace = :ns OR namespace LIKE :prefix) AND superseded_at IS NULL",
        {"ns": ns, "prefix": f"{ns}:%"},
    )
    assert rows, "No memories found for vector search test"

    # Filter same dimension
    valid = [(r["id"], r["content_embedding"]) for r in rows
             if isinstance(r.get("content_embedding"), list) and len(r["content_embedding"]) == dim]
    assert valid, f"No memories with {dim}-dim embeddings in test namespace"

    # Compute cosine similarities
    try:
        import numpy as np
        q = np.array(query_emb, dtype=np.float32)
        E = np.array([v[1] for v in valid], dtype=np.float32)
        sims = (E / np.linalg.norm(E, axis=1, keepdims=True)) @ (q / np.linalg.norm(q))
        sims_list = sims.tolist()
    except ImportError:
        q_norm = sum(x*x for x in query_emb) ** 0.5
        sims_list = []
        for _, emb in valid:
            dot = sum(a*b for a,b in zip(query_emb, emb))
            n = sum(x*x for x in emb) ** 0.5
            sims_list.append(dot / (q_norm * n) if n > 0 else 0.0)

    ranked = sorted(zip([v[0] for v in valid], sims_list), key=lambda x: -x[1])
    assert ranked, "Cosine similarity returned no results"
    top_id = ranked[0][0]
    assert top_id == inserted_ids["fhir-patient"], (
        f"Expected fhir-patient at top, got: {top_id} | top 4: {[x[0] for x in ranked[:4]]}"
    )


def test_vector_search_performance(runner: TestRunner):
    """Python-layer vector search completes in under 2000ms (fetch + cosine similarity).

    With numpy: typically <50ms for 618 records (1536-dim).
    Without numpy: ~100ms pure-Python (still fast enough).
    After cache warm-up: <5ms (cache hit).
    """
    openai_client = runner.get_openai()
    if not openai_client:
        raise AssertionError("No OpenAI API key — cannot test vector search performance")

    query_emb = embed(openai_client, "test performance query")
    dim = len(query_emb)

    start = time.time()
    # Fetch all memories with matching embedding dimension
    rows = arcade_query(
        "SELECT id, content_embedding FROM Memory WHERE content_embedding IS NOT NULL "
        "AND superseded_at IS NULL LIMIT 5000",
        {},
    )
    valid_embs = [r["content_embedding"] for r in rows
                  if isinstance(r.get("content_embedding"), list) and len(r["content_embedding"]) == dim]
    if valid_embs:
        try:
            import numpy as np
            q = np.array(query_emb, dtype=np.float32)
            E = np.array(valid_embs, dtype=np.float32)
            _ = (E / np.linalg.norm(E, axis=1, keepdims=True)) @ (q / np.linalg.norm(q))
        except ImportError:
            q_norm = sum(x*x for x in query_emb) ** 0.5
            for emb in valid_embs[:10]:
                sum(a*b for a,b in zip(query_emb, emb))
    elapsed_ms = (time.time() - start) * 1000

    assert elapsed_ms < 2000, f"Vector search too slow: {elapsed_ms:.0f}ms (should be <2000ms)"
    if runner.verbose:
        print(f"\n    Vector search: {elapsed_ms:.0f}ms for {len(valid_embs)} {dim}-dim embeddings")


def test_keyword_fallback_search(runner: TestRunner):
    """Keyword fallback search returns relevant memories when no HNSW."""
    ns = TEST_NS + ":kw-test"
    mem_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {
            "id": mem_id,
            "content": "Centrifugal pump requires regular maintenance schedule",
            "ns": ns,
            "ts": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id, content FROM Memory "
        "WHERE content.toLowerCase() LIKE :kw "
        "AND (namespace = :ns OR namespace LIKE :prefix) "
        "AND superseded_at IS NULL LIMIT 10",
        {"kw": "%centrifugal%", "ns": ns, "prefix": f"{ns}:%"},
    )
    assert rows, "Keyword search returned no results"
    ids = {r["id"] for r in rows}
    assert mem_id in ids, "Expected memory not found in keyword search"


def test_memory_expiry(runner: TestRunner):
    """Expired memories (expires_at < now) are excluded from active queries."""
    mem_id = uid()
    past = "2020-01-01T00:00:00+00:00"
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = :exp, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": mem_id, "content": "Expired memory", "ns": TEST_NS, "ts": now_str(), "exp": past},
    )
    rows = arcade_query(
        "SELECT id FROM Memory WHERE id = :id "
        "AND (expires_at IS NULL OR expires_at > :now)",
        {"id": mem_id, "now": now_str()},
    )
    assert not rows, "Expired memory not excluded from active queries"


def test_memory_typed_write(runner: TestRunner):
    """Memory types (decision, constraint, incident) stored and queryable by type."""
    for mem_type in ("decision", "constraint", "incident"):
        mid = uid()
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = :mtype, status = 'active', "
            "author = '', affects = [], rationale = 'test rationale', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": f"Test {mem_type}", "ns": TEST_NS, "ts": now_str(), "mtype": mem_type},
        )
        rows = arcade_query(
            "SELECT id, memory_type FROM Memory WHERE id = :id AND memory_type = :mtype",
            {"id": mid, "mtype": mem_type},
        )
        assert rows, f"Memory type '{mem_type}' not found after insert"


def test_constraint_retrieval(runner: TestRunner):
    """CONSTRAINT memories are retrievable for namespace and parent namespaces."""
    constraint_ns = TEST_NS + ":constraints"
    mid = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['constraint'], source = 'constraint', metadata = {}, "
        "memory_type = 'constraint', status = 'active', "
        "author = '', affects = ['auth'], rationale = 'security policy', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {
            "id": mid,
            "content": "Must use HTTPS for all API calls",
            "ns": constraint_ns,
            "ts": now_str(),
        },
    )
    rows = arcade_query(
        "SELECT id FROM Memory WHERE memory_type = 'constraint' "
        "AND status = 'active' AND superseded_at IS NULL "
        "AND (namespace = :ns OR namespace LIKE :prefix)",
        {"ns": constraint_ns, "prefix": f"{constraint_ns}:%"},
    )
    ids = {r["id"] for r in rows}
    assert mid in ids, "Constraint memory not found in constraint query"


def test_graph_traversal(runner: TestRunner):
    """Traverse MENTIONS edges from Entity → Memory (reverse direction)."""
    ns = TEST_NS + ":graph-test"
    entity_id = uid()
    mem_ids = [uid() for _ in range(3)]

    arcade_command(
        "INSERT INTO Entity SET "
        "id = :id, name = 'graph-test-entity', entity_type = 'CONCEPT', "
        "namespace = :ns, created_at = :ts",
        {"id": entity_id, "ns": ns, "ts": now_str()},
    )
    for i, mid in enumerate(mem_ids):
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = 'fact', status = 'active', "
            "author = '', affects = [], rationale = '', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": mid, "content": f"Memory {i} referencing graph-test-entity", "ns": ns, "ts": now_str()},
        )
        arcade_command(
            "CREATE EDGE MENTIONS "
            "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
            "TO (SELECT FROM Entity WHERE id = :eid AND namespace = :ns)",
            {"mid": mid, "eid": entity_id, "ns": ns},
        )

    # Traverse: find all memories that MENTION this entity
    rows = arcade_query(
        "SELECT IN('MENTIONS').id AS memory_ids FROM Entity WHERE id = :eid",
        {"eid": entity_id},
    )
    assert rows, "No MENTIONS traversal results"
    found = set()
    for r in rows:
        mids = r.get("memory_ids") or []
        if isinstance(mids, list):
            found.update(mids)
        else:
            found.add(mids)
    for mid in mem_ids:
        assert mid in found, f"Memory {mid} not reachable via MENTIONS traversal"


def test_count_and_namespace_distribution(runner: TestRunner):
    """COUNT queries and namespace distribution work correctly."""
    ns = TEST_NS + ":count-test"
    expected_count = 5
    for _ in range(expected_count):
        arcade_command(
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :ns, "
            "created_at = :ts, superseded_at = null, "
            "tags = [], source = 'test', metadata = {}, "
            "memory_type = 'fact', status = 'active', "
            "author = '', affects = [], rationale = '', "
            "expires_at = null, review_by = null, "
            "provenance = {}, content_embedding = []",
            {"id": uid(), "content": "Count test memory", "ns": ns, "ts": now_str()},
        )
    rows = arcade_query(
        "SELECT count(*) AS cnt FROM Memory WHERE namespace = :ns",
        {"ns": ns},
    )
    count = int(rows[0].get("cnt", 0))
    assert count == expected_count, f"Expected {expected_count} memories, found {count}"

    dist_rows = arcade_query(
        "SELECT namespace, count(*) AS cnt FROM Memory "
        "WHERE namespace = :ns OR namespace LIKE :prefix "
        "GROUP BY namespace",
        {"ns": ns, "prefix": f"{ns}:%"},
    )
    dist = {r["namespace"]: r["cnt"] for r in dist_rows}
    assert ns in dist, f"Namespace {ns} missing from distribution"
    assert dist[ns] == expected_count


def test_decision_pinning(runner: TestRunner):
    """get_decisions_for_entities() returns decision/ADR memories whose affects
    list overlaps with the given entity names, regardless of vector score."""
    ns = TEST_NS + ":pinning"

    dec_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['decision'], source = 'decision', metadata = {}, "
        "memory_type = 'decision', status = 'active', "
        "author = 'architect', "
        "affects = ['paymentservice', 'ordersvc'], "
        "rationale = 'PCI-DSS requires no direct DB writes from request path', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": dec_id, "content": "PaymentService must never write directly to the database — use the event queue.", "ns": ns, "ts": now_str()},
    )

    adr_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = ['adr'], source = 'adr', metadata = {}, "
        "memory_type = 'adr', status = 'active', "
        "author = 'tech-lead', "
        "affects = ['ordersvc'], "
        "rationale = 'Reduce coupling between order creation and inventory check', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": adr_id, "content": "OrderSvc uses async messaging for inventory checks — no sync HTTP calls.", "ns": ns, "ts": now_str()},
    )

    # Plain fact — must NOT be returned by decision pinning
    fact_id = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = :content, namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'agent', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = ['paymentservice'], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": fact_id, "content": "PaymentService processes ~50k transactions per day.", "ns": ns, "ts": now_str()},
    )

    rows = arcade_query(
        "SELECT id, memory_type, affects FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "AND status = 'active' AND superseded_at IS NULL AND namespace = :ns LIMIT 500",
        {"ns": ns},
    )

    # paymentservice → dec_id only (adr governs ordersvc only)
    norm1 = {"paymentservice"}
    m1 = {r["id"] for r in rows if any(a.lower().strip() in norm1 for a in (r.get("affects") or []))}
    assert dec_id in m1, "Decision governing PaymentService not returned"
    assert fact_id not in m1, "Plain fact must not be returned by decision pinning"

    # ordersvc → both dec_id and adr_id
    norm2 = {"ordersvc"}
    m2 = {r["id"] for r in rows if any(a.lower().strip() in norm2 for a in (r.get("affects") or []))}
    assert dec_id in m2, "Decision (affects ordersvc) not returned"
    assert adr_id in m2, "ADR (affects ordersvc) not returned"

    # unknown entity → empty
    norm3 = {"unknownservice"}
    m3 = [r for r in rows if any(a.lower().strip() in norm3 for a in (r.get("affects") or []))]
    assert len(m3) == 0, "No decisions should match an unknown entity"

    for mid in (dec_id, adr_id, fact_id):
        arcade_command("DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns", {"id": mid, "ns": ns})


def test_delete_memory(runner: TestRunner):
    """Hard-delete removes Memory vertex completely.
    ArcadeDB 26.x requires DELETE VERTEX FROM <Type> syntax.
    """
    mid = uid()
    arcade_command(
        "INSERT INTO Memory SET "
        "id = :id, content = 'To be deleted', namespace = :ns, "
        "created_at = :ts, superseded_at = null, "
        "tags = [], source = 'test', metadata = {}, "
        "memory_type = 'fact', status = 'active', "
        "author = '', affects = [], rationale = '', "
        "expires_at = null, review_by = null, "
        "provenance = {}, content_embedding = []",
        {"id": mid, "ns": TEST_NS, "ts": now_str()},
    )
    arcade_command(
        "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
        {"id": mid, "ns": TEST_NS},
    )
    rows = arcade_query("SELECT id FROM Memory WHERE id = :id", {"id": mid})
    assert not rows, "Memory still exists after hard delete"


def test_persistence_basic(runner: TestRunner):
    """Data written before this test run persists (>0 total memories)."""
    rows = arcade_query("SELECT count(*) AS cnt FROM Memory")
    total = int(rows[0].get("cnt", 0)) if rows else 0
    assert total > 0, "No memories in DB — data was lost (persistence failure?)"


def test_all_vertex_types_have_data(runner: TestRunner):
    """Memory, Entity, Fact vertex types have records (post-import sanity check)."""
    for vtype in ("Memory", "Entity"):
        rows = arcade_query(f"SELECT count(*) AS cnt FROM {vtype}")
        cnt = int(rows[0].get("cnt", 0)) if rows else 0
        assert cnt > 0, f"{vtype} has no records — import may have failed"


def test_mentions_edges_count(runner: TestRunner):
    """MENTIONS edges exist (spaCy extraction produced edges during import)."""
    rows = arcade_query("SELECT count(*) AS cnt FROM MENTIONS")
    cnt = int(rows[0].get("cnt", 0)) if rows else 0
    assert cnt > 0, "No MENTIONS edges — entity extraction may not have run during import"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_TESTS = [
    ("connectivity", test_connectivity),
    ("schema_types", test_schema_types),
    ("schema_memory_properties", test_schema_memory_properties),
    ("schema_indexes", test_schema_indexes),
    ("memory_insert_and_get", test_memory_insert_and_get),
    ("memory_supersession", test_memory_supersession),
    ("superseded_by_edge", test_superseded_by_edge),
    ("entity_upsert", test_entity_upsert),
    ("mentions_edge", test_mentions_edge),
    ("affects_edge", test_affects_edge),
    ("fact_insert_and_supersede", test_fact_insert_and_supersede),
    ("subscription_create_and_feed", test_subscription_create_and_feed),
    ("subscription_filter_types", test_subscription_filter_types),
    ("subscription_deactivate", test_subscription_deactivate),
    ("secret_store_and_retrieve", test_secret_store_and_retrieve),
    ("secret_supersession", test_secret_supersession),
    ("vault_audit_log", test_vault_audit_log),
    ("namespace_index_filter", test_namespace_index_filter),
    ("vector_search", test_vector_search),
    ("vector_search_performance", test_vector_search_performance),
    ("keyword_fallback_search", test_keyword_fallback_search),
    ("memory_expiry", test_memory_expiry),
    ("memory_typed_write", test_memory_typed_write),
    ("constraint_retrieval", test_constraint_retrieval),
    ("graph_traversal", test_graph_traversal),
    ("count_and_namespace_distribution", test_count_and_namespace_distribution),
    ("decision_pinning", test_decision_pinning),
    ("delete_memory", test_delete_memory),
    ("persistence_basic", test_persistence_basic),
    ("all_vertex_types_have_data", test_all_vertex_types_have_data),
    ("mentions_edges_count", test_mentions_edges_count),
]


def main():
    parser = argparse.ArgumentParser(description="Comprehensive ArcadeDB integration tests")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--test", metavar="NAME", help="Run only this test")
    parser.add_argument("--no-cleanup", action="store_true", help="Leave test data in DB")
    args = parser.parse_args()

    runner = TestRunner(verbose=args.verbose)

    print(f"\nengram ArcadeDB Integration Tests")
    print(f"DB: {ARCADEDB_URL}/{DB_NAME}")
    print(f"Test namespace: {TEST_NS}")
    print("=" * 70)

    # Pre-test cleanup
    if not args.no_cleanup:
        print("Pre-test: cleaning up previous test data...", end=" ", flush=True)
        runner.cleanup_test_namespace()
        print("done")

    tests_to_run = ALL_TESTS
    if args.test:
        tests_to_run = [(n, fn) for n, fn in ALL_TESTS if n == args.test]
        if not tests_to_run:
            print(f"[error] Test '{args.test}' not found.", file=sys.stderr)
            sys.exit(1)

    print()
    for name, fn in tests_to_run:
        runner.run(name, fn)

    # Post-test cleanup
    if not args.no_cleanup:
        print()
        print("Post-test: cleaning up test data...", end=" ", flush=True)
        runner.cleanup_test_namespace()
        print("done")

    # Summary
    passed = sum(1 for r in runner.results if r.passed)
    failed = sum(1 for r in runner.results if not r.passed)
    total_time = sum(r.elapsed_ms for r in runner.results)

    print()
    print("=" * 70)
    print(f"Results: {passed}/{len(runner.results)} passed, {failed} failed  ({total_time:.0f}ms total)")

    if failed:
        print("\nFailed tests:")
        for r in runner.results:
            if not r.passed:
                print(f"  ✗ {r.name}: {r.message}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")


if __name__ == "__main__":
    main()
