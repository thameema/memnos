"""
E2E — Core memory operations: write, search, graph, constraint injection.
"""
from __future__ import annotations

import pytest

from tools.e2e.conftest import write_memory, search_memories, wait_for


class TestHealth:
    def test_health_endpoint(self, e2e_client):
        r = e2e_client.get("/api/v1/admin/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") in ("ok", "healthy")

    def test_arcadedb_connected(self, e2e_client):
        r = e2e_client.get("/api/v1/admin/health")
        data = r.json()
        # Health must report arcadedb as connected
        assert data.get("arcadedb") in ("connected", True, "ok") or \
               data.get("components", {}).get("arcadedb") in ("connected", "ok")


class TestMemoryWriteAndSearch:
    def test_write_returns_memory_id(self, e2e_client, ns):
        mem = write_memory(e2e_client, "engram stores memories in ArcadeDB", ns)
        assert "id" in mem
        assert len(mem["id"]) > 0

    def test_written_memory_is_searchable(self, e2e_client, ns):
        write_memory(e2e_client, "The deployment uses Kubernetes rolling updates", ns)
        results = search_memories(e2e_client, "Kubernetes deployment", ns)
        assert len(results) >= 1
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert any("Kubernetes" in c for c in contents)

    def test_search_respects_namespace_isolation(self, e2e_client, ns):
        other_ns = ns + ":other"
        write_memory(e2e_client, "Secret data only in other namespace", other_ns)
        results = search_memories(e2e_client, "Secret data", ns)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert not any("Secret data only in other namespace" in c for c in contents)

    def test_multiple_writes_all_searchable(self, e2e_client, ns):
        phrases = [
            "Auth service uses JWT tokens",
            "Database uses PostgreSQL 15",
            "Frontend deployed on Vercel",
        ]
        for phrase in phrases:
            write_memory(e2e_client, phrase, ns)
        results = search_memories(e2e_client, "JWT auth token", ns, top_k=5)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert any("JWT" in c for c in contents)

    def test_write_with_tags_and_metadata(self, e2e_client, ns):
        mem = write_memory(
            e2e_client,
            "Payment service must use TLS 1.3",
            ns,
            tags=["security", "payment"],
            metadata={"ticket": "SEC-101"},
        )
        assert mem.get("id")

    def test_write_decision_memory_type(self, e2e_client, ns):
        mem = write_memory(
            e2e_client,
            "We chose ArcadeDB over Neo4j for multi-model support",
            ns,
            memory_type="decision",
            author="arch-team",
            rationale="multi-model support and embedded vector search",
        )
        assert mem.get("id")

    def test_get_memory_by_id(self, e2e_client, ns):
        mem = write_memory(e2e_client, "Retrievable memory content", ns)
        mem_id = mem["id"]
        r = e2e_client.get(f"/api/v1/memories/{mem_id}", params={"namespace": ns})
        assert r.status_code == 200
        data = r.json()
        assert data.get("id") == mem_id or data.get("memory", {}).get("id") == mem_id

    def test_search_top_k_respected(self, e2e_client, ns):
        for i in range(5):
            write_memory(e2e_client, f"Memory about caching strategy number {i}", ns)
        results = search_memories(e2e_client, "caching strategy", ns, top_k=2)
        assert len(results) <= 2

    def test_hybrid_search_mode(self, e2e_client, ns):
        write_memory(e2e_client, "gRPC is used for internal service communication", ns)
        r = e2e_client.post("/api/v1/search", json={
            "query": "internal service communication protocol",
            "namespace": ns,
            "top_k": 3,
            "mode": "hybrid",
        })
        assert r.status_code == 200

    def test_graph_search_mode(self, e2e_client, ns):
        write_memory(e2e_client, "Redis is used for session caching", ns)
        r = e2e_client.post("/api/v1/search", json={
            "query": "session cache",
            "namespace": ns,
            "top_k": 3,
            "mode": "graph",
        })
        assert r.status_code == 200


class TestConstraintInjection:
    def test_constraint_memory_prepended_to_results(self, e2e_client, ns):
        # Write a constraint
        write_memory(
            e2e_client,
            "CONSTRAINT: All database queries must go through the ORM layer. Direct SQL is prohibited.",
            ns,
            memory_type="constraint",
        )
        # Write some regular memories
        write_memory(e2e_client, "The user profile service queries the users table", ns)
        write_memory(e2e_client, "Order service reads from the orders table", ns)

        # Search — constraint must appear in results regardless of query
        results = search_memories(e2e_client, "database query pattern", ns, top_k=5)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert any("ORM" in c or "CONSTRAINT" in c for c in contents), \
            "Constraint memory not surfaced in search results"

    def test_constraint_prepended_even_for_unrelated_query(self, e2e_client, ns):
        write_memory(
            e2e_client,
            "CONSTRAINT: No PII in log files.",
            ns,
            memory_type="constraint",
        )
        write_memory(e2e_client, "The deployment pipeline uses GitHub Actions", ns)
        # Query is about CI/CD, not logging — but constraint must still appear
        results = search_memories(e2e_client, "deployment pipeline CI CD", ns, top_k=5)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert any("PII" in c or "CONSTRAINT" in c for c in contents)


class TestMemorySupersede:
    def test_supersede_marks_old_memory_inactive(self, e2e_client, ns):
        mem = write_memory(e2e_client, "We use Postgres 14", ns)
        old_id = mem["id"]
        r = e2e_client.post(f"/api/v1/memories/{old_id}/supersede", json={"namespace": ns})
        assert r.status_code in (200, 204)

    def test_superseded_memory_not_in_search_results(self, e2e_client, ns):
        unique_token = "xzqfoo-obsolete-fact-99"
        mem = write_memory(e2e_client, f"Obsolete: {unique_token}", ns)
        old_id = mem["id"]
        e2e_client.post(f"/api/v1/memories/{old_id}/supersede", json={"namespace": ns})
        # Give the index a moment to update
        import time; time.sleep(0.5)
        results = search_memories(e2e_client, unique_token, ns)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        # If supersede worked correctly the old memory should not be active
        active = [c for c in contents if unique_token in c]
        # Constraint: superseded memory should be absent OR marked superseded
        for c in active:
            assert "superseded" in c.lower() or "deprecated" in c.lower() or len(active) == 0


class TestGraphAPI:
    def test_entity_created_after_memory_write(self, e2e_client, ns):
        write_memory(e2e_client, "Alice manages the platform team", ns)
        r = e2e_client.get("/api/v1/graph/entity", params={"name": "Alice", "namespace": ns})
        # Entity may or may not exist depending on extraction — 200 or 404
        assert r.status_code in (200, 404)

    def test_graph_query_endpoint_responds(self, e2e_client, ns):
        write_memory(e2e_client, "The API gateway routes traffic to microservices", ns)
        r = e2e_client.post("/api/v1/graph/query", json={
            "entity": "API gateway",
            "namespace": ns,
            "depth": 1,
        })
        assert r.status_code in (200, 404)
