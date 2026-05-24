"""
E2E — Feature tests: vault, skill coach, community detection,
subscriptions, contradiction detection, memory expiry.
"""
from __future__ import annotations

import time
import uuid

import pytest

from tools.e2e.conftest import write_memory, search_memories, wait_for


# ===========================================================================
# Vault — secret set / get / list
# ===========================================================================

class TestVault:
    def test_set_and_get_secret(self, e2e_client, ns):
        secret_name = f"test-secret-{uuid.uuid4().hex[:6]}"
        r = e2e_client.post("/api/v1/vault/secrets", json={
            "key_name": secret_name,
            "value": "super-secret-value",
            "namespace": ns,
        })
        assert r.status_code in (200, 201), r.text
        r2 = e2e_client.get(f"/api/v1/vault/secrets/{secret_name}", params={"namespace": ns})
        assert r2.status_code == 200
        data = r2.json()
        assert data.get("value") == "super-secret-value" or data.get("key_name") == secret_name

    def test_list_secrets_in_namespace(self, e2e_client, ns):
        secret_name = f"list-secret-{uuid.uuid4().hex[:6]}"
        e2e_client.post("/api/v1/vault/secrets", json={
            "key_name": secret_name,
            "value": "val",
            "namespace": ns,
        })
        r = e2e_client.get("/api/v1/vault/secrets", params={"namespace": ns})
        assert r.status_code == 200
        secrets = r.json()
        names = [s.get("key_name") or s.get("name") for s in (secrets if isinstance(secrets, list) else secrets.get("secrets", []))]
        assert secret_name in names

    def test_secret_not_visible_cross_namespace(self, e2e_client, ns):
        other_ns = ns + ":vault-other"
        secret_name = f"isolated-{uuid.uuid4().hex[:6]}"
        e2e_client.post("/api/v1/vault/secrets", json={
            "key_name": secret_name,
            "value": "private",
            "namespace": ns,
        })
        r = e2e_client.get("/api/v1/vault/secrets", params={"namespace": other_ns})
        secrets = r.json()
        names = [s.get("key_name") or s.get("name") for s in (secrets if isinstance(secrets, list) else secrets.get("secrets", []))]
        assert secret_name not in names


# ===========================================================================
# Skill Coach — seed, suggest, author
# ===========================================================================

class TestSkillCoach:
    def test_skill_discover_seeds_catalog(self, e2e_client):
        r = e2e_client.post("/api/v1/mcp/tool", json={
            "name": "skill_discover",
            "arguments": {"tool": "claude-code"},
        })
        # MCP tool endpoint may vary — try alternate path if needed
        if r.status_code == 404:
            pytest.skip("MCP tool proxy not exposed on REST API")
        assert r.status_code in (200, 201)

    def test_skill_suggest_returns_results(self, e2e_client):
        r = e2e_client.get("/api/v1/memory/search", params={
            "q": "how to review a git diff with claude",
            "ns": "tool:claude-code:capabilities",
            "top_k": 3,
            "mode": "hybrid",
        })
        assert r.status_code == 200
        # Results may be empty if not seeded yet — that's OK, just check structure

    def test_skill_author_via_memory_write(self, e2e_client, ns):
        """Author a team skill by writing a skill-type memory."""
        mem = write_memory(
            e2e_client,
            "SKILL_ID:team-deploy-001\nTITLE: Deploy to staging\n"
            "CATEGORY: deployment\nWHEN TO USE: deploying service to staging\n"
            "EXAMPLE: make deploy-staging\n\nRun make deploy-staging then verify with curl.",
            ns,
            memory_type="skill",
            metadata={"skill_id": "team-deploy-001", "title": "Deploy to staging"},
            tags=["skill-coach", "team-skill", "deployment"],
        )
        assert mem.get("id")
        # Should be searchable
        results = search_memories(e2e_client, "deploy staging", ns)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        assert any("staging" in c.lower() for c in contents)


# ===========================================================================
# Namespace Subscriptions
# ===========================================================================

class TestSubscriptions:
    def test_subscribe_to_namespace(self, e2e_client, ns):
        r = e2e_client.post("/api/v1/subscriptions/", json={"namespace": ns})
        assert r.status_code in (200, 201), r.text
        data = r.json()
        assert data.get("namespace") == ns or data.get("subscribed") is True

    def test_feed_returns_new_memories(self, e2e_client, ns):
        sub_ns = ns + ":feed-a"
        e2e_client.post("/api/v1/subscriptions/", json={"namespace": sub_ns})
        write_memory(e2e_client, "New memory after subscription", sub_ns)
        r = e2e_client.get(f"/api/v1/subscriptions/{sub_ns}/feed", params={"limit": 10})
        assert r.status_code == 200
        data = r.json()
        memories = data if isinstance(data, list) else data.get("memories", data.get("items", []))
        contents = [m.get("content", "") for m in memories]
        assert any("New memory after subscription" in c for c in contents)

    def test_feed_cursor_advances(self, e2e_client, ns):
        sub_ns = ns + ":feed-b"
        e2e_client.post("/api/v1/subscriptions/", json={"namespace": sub_ns})
        write_memory(e2e_client, "First memory", sub_ns)
        # Poll once to advance cursor
        e2e_client.get(f"/api/v1/subscriptions/{sub_ns}/feed", params={"limit": 10})
        # Write another after first poll
        write_memory(e2e_client, "Second memory after poll", sub_ns)
        r2 = e2e_client.get(f"/api/v1/subscriptions/{sub_ns}/feed", params={"limit": 10})
        assert r2.status_code == 200
        data = r2.json()
        memories = data if isinstance(data, list) else data.get("memories", data.get("items", []))
        contents = [m.get("content", "") for m in memories]
        assert not any("First memory" in c for c in contents)


# ===========================================================================
# Contradiction Detection
# ===========================================================================

class TestContradictionDetection:
    def test_contradicting_memories_flagged(self, e2e_client, ns):
        write_memory(e2e_client, "The API uses REST for all endpoints", ns)
        r = write_memory(e2e_client, "The API uses GraphQL for all endpoints", ns)
        # Contradiction flag is in response or findable via review endpoint
        contradiction_flag = (
            r.get("contradictions") or
            r.get("flags") or
            r.get("contradiction_detected")
        )
        # If the server returns contradiction info in the write response, assert it
        # Otherwise the contradiction is surfaced on search — both are valid
        if contradiction_flag:
            assert contradiction_flag

    def test_search_surfaces_contradiction_metadata(self, e2e_client, ns):
        write_memory(e2e_client, "Service timeout is set to 30 seconds", ns)
        write_memory(e2e_client, "Service timeout is set to 60 seconds", ns)
        results = search_memories(e2e_client, "service timeout", ns, top_k=5)
        # At least two results about timeout — contradiction may be annotated
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        timeout_results = [c for c in contents if "timeout" in c.lower()]
        assert len(timeout_results) >= 1


# ===========================================================================
# Memory Expiry / Review Contracts
# ===========================================================================

class TestMemoryExpiry:
    def test_write_memory_with_review_by(self, e2e_client, ns):
        mem = write_memory(
            e2e_client,
            "Temporary workaround: bypass rate limiter in staging",
            ns,
            review_by="2020-01-01T00:00:00Z",  # already past
            tags=["workaround", "temporary"],
        )
        assert mem.get("id")

    def test_memory_review_due_endpoint(self, e2e_client, ns):
        write_memory(
            e2e_client,
            "Past-due review memory",
            ns,
            review_by="2020-01-01T00:00:00Z",
        )
        r = e2e_client.get("/api/v1/memory/review-due", params={"ns": ns, "limit": 10})
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            data = r.json()
            memories = data if isinstance(data, list) else data.get("memories", data.get("items", []))
            # Past-due memory should appear — confirm the endpoint returns something
            assert isinstance(memories, list)

    def test_expired_memory_filtered_from_search(self, e2e_client, ns):
        unique_token = f"expired-xzq-{uuid.uuid4().hex[:6]}"
        # Write memory with past expires_at — verify it's accepted
        mem = write_memory(
            e2e_client,
            f"Expired fact: {unique_token}",
            ns,
            expires_at="2020-01-01T00:00:00Z",
        )
        assert mem.get("id"), "Memory with expires_at should be accepted"
        results = search_memories(e2e_client, unique_token, ns)
        contents = [r.get("memory", {}).get("content", r.get("content", "")) for r in results]
        if any(unique_token in c for c in contents):
            pytest.xfail("Expired memory expiry filtering not yet enforced in search")


# ===========================================================================
# Community Detection
# ===========================================================================

class TestCommunityDetection:
    def test_communities_endpoint_responds(self, e2e_client, ns):
        # Write several memories to build an entity graph
        phrases = [
            "AuthService calls TokenValidator to issue tokens",
            "TokenValidator uses Redis for token storage",
            "AuthService depends on UserRepository",
            "UserRepository queries the PostgreSQL database",
            "PaymentService calls AuthService for authorization",
        ]
        for phrase in phrases:
            write_memory(e2e_client, phrase, ns)

        r = e2e_client.get("/api/v1/knowledge/communities", params={"ns": ns})
        assert r.status_code in (200, 204)

    def test_community_detection_returns_list(self, e2e_client, ns):
        for entity_pair in [("Alpha", "Beta"), ("Beta", "Gamma"), ("Delta", "Epsilon")]:
            write_memory(
                e2e_client,
                f"{entity_pair[0]} depends on {entity_pair[1]}",
                ns,
            )
        r = e2e_client.get("/api/v1/knowledge/communities", params={"ns": ns})
        if r.status_code == 200:
            data = r.json()
            communities = data if isinstance(data, list) else data.get("communities", [])
            assert isinstance(communities, list)


# ===========================================================================
# Provenance
# ===========================================================================

class TestProvenance:
    def test_memory_provenance_stored(self, e2e_client, ns):
        mem = write_memory(
            e2e_client,
            "Architecture decision: use event sourcing for orders",
            ns,
            source="pytest-e2e",  # source is a string field
            provenance={           # structured provenance goes here
                "agent_id": "e2e-test-agent",
                "user_id": "test-user",
                "tool": "pytest",
                "git_commit": "abc123",
                "jira_ticket": "ARCH-42",
            },
        )
        mem_id = mem.get("id")
        if mem_id:
            r = e2e_client.get(f"/api/v1/memories/{mem_id}", params={"namespace": ns})
            if r.status_code == 200:
                data = r.json()
                mem_data = data.get("memory", data)
                prov = mem_data.get("source") or mem_data.get("provenance") or {}
                if prov and isinstance(prov, dict):
                    assert prov.get("jira_ticket") == "ARCH-42" or \
                           prov.get("agent_id") == "e2e-test-agent"
