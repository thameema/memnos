"""
Feature 1 — FastEmbed/ONNX with nomic-embed-text-v1.5.

These are e2e tests against the live dev memnos. We treat the embedder as
an opaque service and verify the *contract* it must uphold:

  1. Writing a memory and searching for a paraphrase of it must return it
     near the top — confirms the embedder is loaded and producing
     semantically meaningful vectors.
  2. Two semantically-distant memories in the same namespace must rank
     correctly when queried — confirms the vector space discriminates
     content, not just memorises text.

We do NOT pin the exact similarity score, because the score depends on the
specific model and dimensions. We only assert *ordering* — which survives
model swaps.
"""
from __future__ import annotations


def _post_memory(http, ns: str, content: str, memory_type: str = "fact") -> dict:
    r = http.post(
        "/api/v1/memory/",
        json={"content": content, "namespace": ns, "memory_type": memory_type},
    )
    assert r.status_code == 201, f"POST /memory/ failed: {r.status_code} {r.text}"
    return r.json()


def _search(http, ns: str, q: str, mode: str = "vector", top_k: int = 5) -> list[dict]:
    r = http.get(
        "/api/v1/memory/search",
        params={"q": q, "ns": ns, "mode": mode, "top_k": top_k},
    )
    assert r.status_code == 200, f"GET /memory/search failed: {r.status_code} {r.text}"
    return r.json()


class TestEmbedderRoundtrip:
    """A paraphrase query should retrieve the seed memory."""

    def test_paraphrase_retrieves_seed(self, http, ns_embed: str):
        seed = _post_memory(
            http, ns_embed,
            "Decision: chose PostgreSQL over MongoDB for analytical workloads",
            memory_type="decision",
        )
        results = _search(http, ns_embed, "which database did we pick for analytics", mode="vector")
        assert results, "vector search returned no results — embedder may not be loaded"
        assert results[0]["id"] == seed["id"], (
            f"top result is not the seed: top={results[0]['content']!r}"
        )

    def test_unrelated_query_does_not_promote_seed(self, http, ns_embed: str):
        seed = _post_memory(
            http, ns_embed,
            "Decision: chose PostgreSQL over MongoDB for analytical workloads",
            memory_type="decision",
        )
        noise = _post_memory(
            http, ns_embed,
            "Provider directory sync runs nightly at 02:00 UTC against PECOS",
        )
        results = _search(http, ns_embed, "nightly provider directory sync", mode="vector")
        assert results, "vector search returned no results"
        # The PECOS memory must rank above the PostgreSQL decision for this query.
        ranks = {r["id"]: i for i, r in enumerate(results)}
        assert ranks.get(noise["id"], 999) < ranks.get(seed["id"], 999), (
            "embedder failed to discriminate: PECOS query promoted DB decision"
        )


class TestEmbedderDimensions:
    """nomic-embed-text-v1.5 (default for FastEmbed) is 768-dim. We can't
    query the embedder directly through the public API, but two writes in
    the same namespace succeeding implies the vector backend accepts the
    dimension we're producing — which is the externally observable contract.
    """

    def test_two_writes_succeed_in_same_namespace(self, http, ns_embed: str):
        first = _post_memory(http, ns_embed, "alpha bravo charlie")
        second = _post_memory(http, ns_embed, "delta echo foxtrot")
        assert first["id"] != second["id"]
        # Round-trip both via search
        results = _search(http, ns_embed, "alpha bravo charlie", mode="vector")
        ids = {r["id"] for r in results}
        assert first["id"] in ids, "first write was not retrievable"
