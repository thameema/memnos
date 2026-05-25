"""
engram v0.2 pytest test suite — REST API coverage.

What this suite covers
----------------------
- Health / root endpoints
- Memory write: field presence, entity extraction trigger, credential redaction,
  namespace isolation
- Memory search: semantic results, top_k enforcement, empty query handling
- Knowledge graph: visualize returns Memory + Entity nodes, edge integrity (no
  dangling endpoints), stats edge_count, namespace distribution in stats
- Vault CRUD: set/get round-trip, list hides values, rotate, audit log, wrong
  namespace returns 404
- Entity extraction: entity count increases after write, edge count increases,
  entity names are lowercase
- Visualize edge integrity parametrized over multiple namespaces

Configuration
-------------
Export env vars before running (defaults shown):
    ENGRAM_BASE_URL=http://127.0.0.1:8766
    ENGRAM_API_KEY=engram-local-dev-key

Run:
    python -m pytest tools/test_engram.py -v
    python -m pytest tools/test_engram.py -v -m "not slow"   # skip entity tests
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

BASE_URL: str = os.environ.get("ENGRAM_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
API_KEY: str = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")

# Unique run prefix so every test session uses isolated namespaces and no
# cross-run pollution occurs.  No cleanup needed — namespaces are cheap.
_RUN_ID: str = uuid.uuid4().hex[:8]
TEST_NS: str = f"test:engram:{_RUN_ID}"


# ---------------------------------------------------------------------------
# Shared httpx client fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client() -> httpx.Client:
    """A sync httpx client pre-loaded with auth headers, reused across tests."""
    with httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30.0,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: write a memory via the REST API
# ---------------------------------------------------------------------------

def write_memory(
    client: httpx.Client,
    content: str,
    namespace: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """POST /api/v1/memory/ and return the parsed JSON response."""
    payload: dict[str, Any] = {"content": content, "namespace": namespace}
    if tags is not None:
        payload["tags"] = tags
    resp = client.post("/api/v1/memory/", json=payload)
    resp.raise_for_status()
    return resp.json()


# ===========================================================================
# TestHealthCheck
# ===========================================================================

class TestHealthCheck:
    """Verify that the server and ArcadeDB backend are reachable."""

    def test_root_responds(self, client: httpx.Client) -> None:
        """GET / should return a JSON body identifying the service as 'engram'."""
        resp = client.get("/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("service") == "engram", (
            f"Expected service='engram', got: {body}"
        )

    def test_health_ok(self, client: httpx.Client) -> None:
        """GET /api/v1/admin/health should report status=ok and arcadedb.status=ok."""
        resp = client.get("/api/v1/admin/health")
        assert resp.status_code == 200, f"Health check failed: {resp.text}"
        body = resp.json()
        assert body.get("status") == "ok", f"Overall status not 'ok': {body}"
        # arcadedb field may be a string "ok" or a dict {"status": "ok"}
        arcadedb_field = body.get("arcadedb")
        if isinstance(arcadedb_field, dict):
            assert arcadedb_field.get("status") == "ok", (
                f"arcadedb.status not 'ok': {body}"
            )
        else:
            assert arcadedb_field == "ok", (
                f"arcadedb field expected 'ok', got: {arcadedb_field!r}"
            )


# ===========================================================================
# TestMemoryWrite
# ===========================================================================

class TestMemoryWrite:
    """Test POST /api/v1/memory/."""

    def test_write_returns_id(self, client: httpx.Client) -> None:
        """A successful write must return id, content, namespace, and created_at."""
        ns = f"{TEST_NS}:write-basic"
        data = write_memory(client, "engram write test — basic field check", ns, tags=["test"])
        assert "id" in data and data["id"], f"Missing 'id' in response: {data}"
        assert "content" in data, f"Missing 'content' in response: {data}"
        assert "namespace" in data, f"Missing 'namespace' in response: {data}"
        assert "created_at" in data, f"Missing 'created_at' in response: {data}"
        assert data["namespace"] == ns

    @pytest.mark.slow
    def test_write_creates_entity_edges(self, client: httpx.Client) -> None:
        """Writing content with known tech terms should produce edges in the graph."""
        ns = f"{TEST_NS}:write-edges"
        write_memory(
            client,
            "kubernetes and docker are container orchestration technologies",
            ns,
            tags=["infra"],
        )
        # Allow background spaCy extraction to complete
        time.sleep(2)

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": ns, "limit": 50},
        )
        assert resp.status_code == 200, f"visualize failed: {resp.text}"
        body = resp.json()
        edges = body.get("edges", [])
        assert len(edges) > 0, (
            "Expected at least one edge after writing content with tech entities, "
            f"but got 0. nodes={body.get('nodes', [])}"
        )

    def test_write_detects_credential(self, client: httpx.Client) -> None:
        """Anthropic-style API key patterns must be redacted in stored content."""
        ns = f"{TEST_NS}:write-cred"
        fake_key = "sk-ant-api03-fake123ABCDEF0000000000000000000000000000000000000000000000000"
        data = write_memory(client, f"my api key is {fake_key} do not leak it", ns)
        stored_content: str = data.get("content", "")
        assert fake_key not in stored_content, (
            f"Credential was NOT redacted. Stored content: {stored_content!r}"
        )
        assert "REDACTED" in stored_content.upper(), (
            f"Expected REDACTED placeholder in stored content: {stored_content!r}"
        )

    def test_namespace_isolation(self, client: httpx.Client) -> None:
        """Memory written to ns1 must not appear when visualising ns2."""
        ns1 = f"{TEST_NS}:isolation:ns1"
        ns2 = f"{TEST_NS}:isolation:ns2"

        written = write_memory(client, "namespace isolation sentinel value alpha bravo", ns1)
        written_id = written.get("id", "")

        # Give ArcadeDB a moment to commit
        time.sleep(1)

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": ns2, "limit": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        node_ids = {n.get("id") for n in body.get("nodes", [])}
        assert written_id not in node_ids, (
            f"Memory {written_id} from ns1 leaked into ns2 visualize response"
        )


# ===========================================================================
# TestMemorySearch
# ===========================================================================

class TestMemorySearch:
    """Test GET /api/v1/memory/search."""

    def test_search_returns_results(self, client: httpx.Client) -> None:
        """After writing, a semantically matching query should return results."""
        ns = f"{TEST_NS}:search-basic"
        write_memory(
            client,
            "ArcadeDB is a multi-model graph database with vector support",
            ns,
            tags=["db"],
        )
        time.sleep(1)

        resp = client.get(
            "/api/v1/memory/search",
            params={"q": "graph database", "ns": ns, "top_k": 5, "mode": "hybrid"},
        )
        assert resp.status_code == 200, f"Search failed: {resp.text}"
        results = resp.json()
        assert isinstance(results, list), f"Expected list, got {type(results).__name__}"
        assert len(results) > 0, "Expected at least one search result"
        # Confirm the written content is represented somewhere
        all_content = " ".join(r.get("content", "") for r in results)
        assert "ArcadeDB" in all_content or "graph" in all_content.lower(), (
            f"Expected ArcadeDB/graph in results but got: {all_content[:300]}"
        )

    def test_search_top_k_respected(self, client: httpx.Client) -> None:
        """Search results must not exceed the requested top_k."""
        ns = f"{TEST_NS}:search-topk"
        for i in range(5):
            write_memory(client, f"top_k test memory entry number {i} with unique content", ns)
        time.sleep(1)

        resp = client.get(
            "/api/v1/memory/search",
            params={"q": "top_k test memory entry", "ns": ns, "top_k": 2, "mode": "hybrid"},
        )
        assert resp.status_code == 200, f"Search failed: {resp.text}"
        results = resp.json()
        assert isinstance(results, list)
        assert len(results) <= 2, (
            f"top_k=2 should return at most 2 results, got {len(results)}"
        )

    def test_search_empty_query_returns_recent(self, client: httpx.Client) -> None:
        """An empty query string must not crash the server and must return a list."""
        ns = f"{TEST_NS}:search-empty"
        write_memory(client, "entry for empty query test", ns)
        time.sleep(1)

        resp = client.get(
            "/api/v1/memory/search",
            params={"q": "", "ns": ns, "top_k": 10, "mode": "hybrid"},
        )
        # Server should respond gracefully (200 or 422 for validation error, but not 500)
        assert resp.status_code in (200, 422), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            assert isinstance(resp.json(), list), "Expected list response for empty query"


# ===========================================================================
# TestKnowledgeGraph
# ===========================================================================

class TestKnowledgeGraph:
    """Test GET /api/v1/graph/visualize and GET /api/v1/graph/stats."""

    @pytest.mark.slow
    def test_visualize_returns_memory_and_entity_nodes(self, client: httpx.Client) -> None:
        """After writing content with named entities, visualize must include both
        type='Memory' and type='Entity' nodes."""
        ns = f"{TEST_NS}:viz-types"
        write_memory(
            client,
            "Terraform provisions AWS infrastructure for Kubernetes clusters on EKS",
            ns,
            tags=["infra"],
        )
        time.sleep(3)  # allow spaCy extraction + ArcadeDB indexing

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": ns, "limit": 100},
        )
        assert resp.status_code == 200, f"visualize failed: {resp.text}"
        body = resp.json()
        nodes = body.get("nodes", [])
        assert len(nodes) > 0, "No nodes returned from visualize"

        node_types = {n.get("type") for n in nodes}
        assert "Memory" in node_types, (
            f"Expected 'Memory' node type in {node_types}"
        )
        # Entity nodes require extraction to have completed
        assert "Entity" in node_types, (
            f"Expected 'Entity' node type after spaCy extraction, got: {node_types}. "
            f"Nodes: {nodes}"
        )

    def test_visualize_no_bad_edges(self, client: httpx.Client) -> None:
        """Every edge's source and target must refer to an existing node in the response."""
        ns = f"{TEST_NS}:viz-edge-check"
        write_memory(client, "edge integrity test entry for graph visualize", ns)
        time.sleep(2)

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": ns, "limit": 200},
        )
        assert resp.status_code == 200, f"visualize failed: {resp.text}"
        body = resp.json()
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])

        node_ids = {n.get("id") for n in nodes if n.get("id")}
        bad_edges = [
            e for e in edges
            if e.get("source") not in node_ids or e.get("target") not in node_ids
        ]
        assert bad_edges == [], (
            f"Found {len(bad_edges)} edge(s) referencing nodes not in node set: {bad_edges}"
        )

    @pytest.mark.slow
    def test_stats_edge_count_nonzero(self, client: httpx.Client) -> None:
        """After writing memories with extractable entities, edge_count must be > 0."""
        ns = f"{TEST_NS}:stats-edges"
        write_memory(
            client,
            "Docker containers run on Kubernetes pods managed by Helm charts",
            ns,
        )
        time.sleep(3)

        resp = client.get("/api/v1/graph/stats", params={"namespace": ns})
        assert resp.status_code == 200, f"stats failed: {resp.text}"
        body = resp.json()
        edge_count = body.get("edge_count", 0)
        assert edge_count > 0, (
            f"Expected edge_count > 0 after writing entity-rich content, got {edge_count}. "
            f"Full stats: {body}"
        )

    def test_stats_namespace_distribution(self, client: httpx.Client) -> None:
        """Memories written to two distinct sub-namespaces should appear in stats
        namespace_distribution for the parent namespace."""
        base_ns = f"{TEST_NS}:stats-nsdist"
        ns_a = f"{base_ns}:alpha"
        ns_b = f"{base_ns}:beta"

        write_memory(client, "namespace distribution test content for alpha", ns_a)
        write_memory(client, "namespace distribution test content for beta", ns_b)
        time.sleep(1)

        # Query the parent namespace — prefix matching should include children
        resp = client.get("/api/v1/graph/stats", params={"namespace": base_ns})
        assert resp.status_code == 200, f"stats failed: {resp.text}"
        body = resp.json()
        dist = body.get("namespace_distribution", [])

        # Extract namespace names from the distribution list
        dist_namespaces = {
            (entry.get("namespace") if isinstance(entry, dict) else entry)
            for entry in dist
        }
        missing = {ns_a, ns_b} - dist_namespaces
        assert not missing, (
            f"Expected both sub-namespaces in distribution, missing: {missing}. "
            f"Got distribution: {dist}"
        )


# ===========================================================================
# TestVault
# ===========================================================================

class TestVault:
    """Test POST/GET/PUT /api/v1/vault/secrets and /api/v1/vault/audit."""

    def _unique_key(self) -> str:
        return f"test-key-{uuid.uuid4().hex[:8]}"

    def test_vault_set_and_get(self, client: httpx.Client) -> None:
        """Set a secret, then GET it back — decrypted value must match."""
        ns = f"{TEST_NS}:vault-basic"
        key_name = self._unique_key()
        secret_value = f"super-secret-{uuid.uuid4().hex}"

        set_resp = client.post(
            "/api/v1/vault/secrets",
            json={
                "key_name": key_name,
                "value": secret_value,
                "namespace": ns,
                "secret_type": "api_key",
            },
        )
        assert set_resp.status_code in (200, 201), (
            f"vault set failed: {set_resp.status_code} {set_resp.text}"
        )

        get_resp = client.get(
            f"/api/v1/vault/secrets/{key_name}",
            params={"namespace": ns},
        )
        assert get_resp.status_code == 200, (
            f"vault get failed: {get_resp.status_code} {get_resp.text}"
        )
        body = get_resp.json()
        assert body.get("value") == secret_value, (
            f"Decrypted value mismatch. Expected {secret_value!r}, got {body.get('value')!r}"
        )

    def test_vault_list_hides_values(self, client: httpx.Client) -> None:
        """Listing secrets must not expose the plaintext value field."""
        ns = f"{TEST_NS}:vault-list"
        key_name = self._unique_key()

        client.post(
            "/api/v1/vault/secrets",
            json={
                "key_name": key_name,
                "value": "do-not-expose-this-value",
                "namespace": ns,
                "secret_type": "token",
            },
        )

        list_resp = client.get("/api/v1/vault/secrets", params={"namespace": ns})
        assert list_resp.status_code == 200, f"vault list failed: {list_resp.text}"
        secrets = list_resp.json()
        assert isinstance(secrets, list)

        for entry in secrets:
            assert "value" not in entry, (
                f"Secret listing must NOT include 'value' field, but got: {entry}"
            )
            assert "ciphertext" not in entry, (
                f"Secret listing must NOT include 'ciphertext', but got: {entry}"
            )

    def test_vault_rotate(self, client: httpx.Client) -> None:
        """Rotate a secret's value and verify the new value is returned on GET."""
        ns = f"{TEST_NS}:vault-rotate"
        key_name = self._unique_key()
        original_value = f"original-{uuid.uuid4().hex}"
        rotated_value = f"rotated-{uuid.uuid4().hex}"

        client.post(
            "/api/v1/vault/secrets",
            json={"key_name": key_name, "value": original_value, "namespace": ns},
        )

        rotate_resp = client.put(
            f"/api/v1/vault/secrets/{key_name}/rotate",
            json={"new_value": rotated_value},
            params={"namespace": ns},
        )
        assert rotate_resp.status_code == 200, (
            f"rotate failed: {rotate_resp.status_code} {rotate_resp.text}"
        )

        get_resp = client.get(
            f"/api/v1/vault/secrets/{key_name}",
            params={"namespace": ns},
        )
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body.get("value") == rotated_value, (
            f"After rotation expected {rotated_value!r}, got {body.get('value')!r}"
        )

    def test_vault_audit_logs_actions(self, client: httpx.Client) -> None:
        """After setting a secret, the audit log must contain at least one entry
        with action='set'."""
        ns = f"{TEST_NS}:vault-audit"
        key_name = self._unique_key()

        client.post(
            "/api/v1/vault/secrets",
            json={"key_name": key_name, "value": "audit-test-value", "namespace": ns},
        )

        audit_resp = client.get("/api/v1/vault/audit", params={"namespace": ns})
        assert audit_resp.status_code == 200, (
            f"audit endpoint failed: {audit_resp.status_code} {audit_resp.text}"
        )
        entries = audit_resp.json()
        assert isinstance(entries, list), f"Expected list from audit, got: {type(entries)}"
        assert len(entries) > 0, "Audit log is empty after vault set operation"

        actions = [e.get("action", "").lower() for e in entries]
        assert any(a in ("set", "write", "create") for a in actions), (
            f"Expected an action='set' entry in audit log, got actions: {actions}"
        )

    def test_vault_wrong_namespace_fails(self, client: httpx.Client) -> None:
        """Requesting a secret from a namespace that does not own it must return 404."""
        ns_owner = f"{TEST_NS}:vault-ns-owner"
        ns_wrong = f"{TEST_NS}:vault-ns-wrong"
        key_name = self._unique_key()

        client.post(
            "/api/v1/vault/secrets",
            json={"key_name": key_name, "value": "owner-only-secret", "namespace": ns_owner},
        )

        get_resp = client.get(
            f"/api/v1/vault/secrets/{key_name}",
            params={"namespace": ns_wrong},
        )
        assert get_resp.status_code == 404, (
            f"Expected 404 for wrong namespace, got {get_resp.status_code}: {get_resp.text}"
        )


# ===========================================================================
# TestEntityExtraction
# ===========================================================================

class TestEntityExtraction:
    """Test that spaCy entity extraction runs correctly after memory writes."""

    @pytest.mark.slow
    def test_entities_created_after_write(self, client: httpx.Client) -> None:
        """Entity count (via stats or visualize) must increase after writing
        content with recognisable named entities."""
        ns = f"{TEST_NS}:entity-create"

        # Baseline: stats before write
        before_resp = client.get("/api/v1/graph/stats", params={"namespace": ns})
        before_count = 0
        if before_resp.status_code == 200:
            before_count = before_resp.json().get("node_count", 0)

        write_memory(
            client,
            "Kubernetes runs on AWS EKS clusters using Terraform infrastructure as code",
            ns,
        )
        time.sleep(3)

        after_resp = client.get("/api/v1/graph/stats", params={"namespace": ns})
        assert after_resp.status_code == 200, f"stats failed: {after_resp.text}"
        after_count = after_resp.json().get("node_count", 0)

        assert after_count > before_count, (
            f"Expected node_count to increase after entity-rich write. "
            f"Before: {before_count}, after: {after_count}"
        )

    @pytest.mark.slow
    def test_mentions_edges_created(self, client: httpx.Client) -> None:
        """Writing entity-rich content should produce MENTIONS edges,
        reflected as edge_count > 0 in stats."""
        ns = f"{TEST_NS}:entity-edges"
        write_memory(
            client,
            "Python and FastAPI power a high-throughput REST API layer on Azure",
            ns,
        )
        time.sleep(3)

        resp = client.get("/api/v1/graph/stats", params={"namespace": ns})
        assert resp.status_code == 200
        edge_count = resp.json().get("edge_count", 0)
        assert edge_count > 0, (
            f"Expected MENTIONS edges in graph after entity write, got edge_count={edge_count}"
        )

    @pytest.mark.slow
    def test_entity_names_lowercased(self, client: httpx.Client) -> None:
        """Entity nodes stored in ArcadeDB must have lowercase names."""
        ns = f"{TEST_NS}:entity-case"
        write_memory(
            client,
            "Kubernetes orchestrates Docker containers on AWS infrastructure",
            ns,
        )
        time.sleep(3)

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": ns, "limit": 100},
        )
        assert resp.status_code == 200
        nodes = resp.json().get("nodes", [])
        entity_nodes = [n for n in nodes if n.get("type") == "Entity"]

        assert len(entity_nodes) > 0, (
            "No Entity nodes found — extraction may not have run. "
            f"All node types: {[n.get('type') for n in nodes]}"
        )
        for node in entity_nodes:
            label = node.get("label", "")
            assert label == label.lower(), (
                f"Entity label is not lowercase: {label!r}"
            )


# ===========================================================================
# TestVisualizationEdgeIntegrity
# ===========================================================================

_INTEGRITY_NAMESPACES = ["all", f"{TEST_NS}:viz-integrity"]


class TestVisualizationEdgeIntegrity:
    """Parametrized edge-integrity checks over several namespaces."""

    @pytest.fixture(autouse=True)
    def _seed_namespace(self, client: httpx.Client) -> None:
        """Write a baseline memory into the test namespace before each test."""
        write_memory(
            client,
            "edge integrity baseline memory for visualization test",
            f"{TEST_NS}:viz-integrity",
        )
        time.sleep(1)

    @pytest.mark.parametrize("namespace", _INTEGRITY_NAMESPACES)
    def test_all_edge_endpoints_present(
        self, client: httpx.Client, namespace: str
    ) -> None:
        """For every namespace, all edge source and target IDs must exist in the
        node set returned by the same visualize call."""
        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": namespace, "limit": 200},
        )
        assert resp.status_code == 200, f"visualize failed for ns={namespace}: {resp.text}"
        body = resp.json()
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])

        if not edges:
            pytest.skip(f"No edges for namespace={namespace!r} — nothing to validate")

        node_ids = {n.get("id") for n in nodes if n.get("id")}
        bad = [
            e for e in edges
            if e.get("source") not in node_ids or e.get("target") not in node_ids
        ]
        assert bad == [], (
            f"Dangling edges (endpoints not in node set) for ns={namespace!r}: {bad}"
        )

    @pytest.mark.parametrize("namespace", [f"{TEST_NS}:viz-integrity"])
    @pytest.mark.slow
    def test_entity_nodes_in_response(
        self, client: httpx.Client, namespace: str
    ) -> None:
        """After extraction, at least one Entity node must appear in the visualize response."""
        # Write a memory with strong named entities to ensure extraction fires
        write_memory(
            client,
            "Kubernetes Docker Terraform AWS are infrastructure keywords for entity test",
            namespace,
        )
        time.sleep(3)

        resp = client.get(
            "/api/v1/graph/visualize",
            params={"namespace": namespace, "limit": 100},
        )
        assert resp.status_code == 200
        nodes = resp.json().get("nodes", [])
        types = {n.get("type") for n in nodes}
        assert "Entity" in types, (
            f"Expected at least one Entity node in visualize for ns={namespace!r}. "
            f"Got types: {types}"
        )
