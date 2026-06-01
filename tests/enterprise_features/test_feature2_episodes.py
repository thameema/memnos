"""
Feature 2 — Immutable Episodes + source_episode_ids lineage.

Contract under test:

  * POST /memory/ auto-creates an Episode and links the new Memory to it
    via source_episode_ids.
  * GET /memory/{id} round-trips source_episode_ids.
  * POST /episodes/ accepts a verbatim input and returns 201.
  * GET /episodes/{id} returns the stored Episode unchanged.
  * GET /episodes/?ns= lists Episodes in the namespace.
  * GET /episodes/{id}/memories returns derived Memories (reverse lookup).
  * PUT / PATCH / DELETE on /episodes/{id} all return 405 — the immutability
    guarantee enforced at the API surface.

These are end-to-end against the live dev memnos, namespace-isolated.
"""
from __future__ import annotations


def _write_memory(http, ns: str, content: str) -> dict:
    r = http.post(
        "/api/v1/memory/",
        json={"content": content, "namespace": ns, "memory_type": "decision"},
    )
    assert r.status_code == 201, f"POST /memory/ failed: {r.status_code} {r.text}"
    return r.json()


def _get_memory(http, ns: str, mid: str) -> dict:
    r = http.get(f"/api/v1/memory/{mid}", params={"ns": ns})
    assert r.status_code == 200, f"GET /memory/{{id}} failed: {r.status_code} {r.text}"
    return r.json()


def _post_episode(http, ns: str, content: str, source: str = "api") -> dict:
    r = http.post(
        "/api/v1/episodes/",
        json={"content": content, "namespace": ns, "source": source},
    )
    assert r.status_code == 201, f"POST /episodes/ failed: {r.status_code} {r.text}"
    return r.json()


class TestAutoEpisodeOnMemoryWrite:
    """Every memory POST silently creates an Episode and links to it."""

    def test_memory_response_carries_source_episode_id(self, http, ns_episode: str):
        mem = _write_memory(http, ns_episode, "auto-link test memory content")
        assert "source_episode_ids" in mem, "MemoryResponse missing source_episode_ids field"
        assert isinstance(mem["source_episode_ids"], list)
        assert len(mem["source_episode_ids"]) == 1, (
            f"expected 1 auto-Episode id, got: {mem['source_episode_ids']}"
        )

    def test_source_episode_id_roundtrips_via_get(self, http, ns_episode: str):
        mem = _write_memory(http, ns_episode, "round-trip episode id test")
        roundtripped = _get_memory(http, ns_episode, mem["id"])
        assert roundtripped["source_episode_ids"] == mem["source_episode_ids"], (
            f"source_episode_ids mismatch on GET: "
            f"wrote={mem['source_episode_ids']} read={roundtripped['source_episode_ids']}"
        )

    def test_auto_episode_appears_in_namespace_listing(self, http, ns_episode: str):
        mem = _write_memory(http, ns_episode, "ensure auto-Ep is listable")
        ep_id = mem["source_episode_ids"][0]
        r = http.get("/api/v1/episodes/", params={"ns": ns_episode, "limit": 50})
        assert r.status_code == 200
        listed_ids = [e["id"] for e in r.json()]
        assert ep_id in listed_ids, "auto-created Episode not visible in list"

    def test_reverse_lookup_episode_to_memories(self, http, ns_episode: str):
        mem = _write_memory(http, ns_episode, "reverse-traversal test memory")
        ep_id = mem["source_episode_ids"][0]
        r = http.get(f"/api/v1/episodes/{ep_id}/memories", params={"ns": ns_episode})
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        derived = r.json()
        derived_ids = [d["id"] for d in derived]
        assert mem["id"] in derived_ids, (
            f"derived memory {mem['id']} missing from /episodes/{{id}}/memories"
        )


class TestDirectEpisodeAPI:
    """Direct Episode creation (file upload, voice, webhook flows)."""

    def test_post_episode_returns_full_envelope(self, http, ns_episode: str):
        ep = _post_episode(http, ns_episode, "verbatim clinical note", source="voice")
        for key in ("id", "content", "namespace", "created_at", "source"):
            assert key in ep, f"Episode response missing field {key!r}"
        assert ep["content"] == "verbatim clinical note"
        assert ep["source"] == "voice"
        assert ep["namespace"] == ns_episode

    def test_get_episode_by_id_roundtrips(self, http, ns_episode: str):
        ep = _post_episode(http, ns_episode, "fetch-by-id roundtrip body")
        r = http.get(f"/api/v1/episodes/{ep['id']}", params={"ns": ns_episode})
        assert r.status_code == 200
        got = r.json()
        assert got["id"] == ep["id"]
        assert got["content"] == ep["content"]


class TestEpisodeImmutability:
    """PUT / PATCH / DELETE must all return 405 on /episodes/{id}."""

    def test_no_put_patch_delete(self, http, ns_episode: str):
        ep = _post_episode(http, ns_episode, "immutable body for verb test")
        for verb in ("PUT", "PATCH", "DELETE"):
            r = http.request(
                verb, f"/api/v1/episodes/{ep['id']}", params={"ns": ns_episode}
            )
            assert r.status_code == 405, (
                f"{verb} /episodes/{{id}} should return 405 (immutable); got {r.status_code}"
            )

    def test_get_after_failed_mutation_returns_original(self, http, ns_episode: str):
        ep = _post_episode(http, ns_episode, "original verbatim content")
        # Try (failing) mutations
        for verb in ("PUT", "PATCH"):
            http.request(
                verb, f"/api/v1/episodes/{ep['id']}",
                params={"ns": ns_episode},
                json={"content": "TAMPERED"},
            )
        # Confirm the body is unchanged
        roundtripped = http.get(
            f"/api/v1/episodes/{ep['id']}", params={"ns": ns_episode},
        ).json()
        assert roundtripped["content"] == "original verbatim content", (
            f"Episode body was mutated despite 405: {roundtripped['content']!r}"
        )
