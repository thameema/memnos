"""
Feature 3 — Hybrid search: vector + BM25 + Reciprocal Rank Fusion.

These are e2e tests against the live dev memnos. We seed a small fixed
corpus into a disposable namespace, then query it in each of the three
search modes and assert ordering properties:

  * mode=vector  — semantic, returns paraphrase matches.
  * mode=bm25    — lexical, exact-token wins regardless of meaning.
  * mode=rrf     — fused, the doc strong in *both* lists wins.

The key qualitative claim of RRF is that it outranks each arm alone on
queries where lexical and semantic signals disagree. Test
``test_rrf_picks_semantic_winner_over_lexical_noise`` is the most important
one in this module — it's the demo case that justifies shipping RRF.

These tests intentionally assert *positions*, not absolute scores, because
RRF scores are tiny (~0.03) and BM25 scores are unbounded — ordering is the
invariant that survives tuning.
"""
from __future__ import annotations

import pytest


_CORPUS = [
    "Customer Centene runs Innovaccer for population health analytics",
    "Decision: replace MongoDB with PostgreSQL for OLAP workloads",
    "Bug HPTE-413: scheduler-service fails on retry after 503 from upstream",
    "Members in California with chronic conditions enrolled in SNP plans",
    "FHIR Patient resource must include MBI in identifier slice",
    "Champion Health Plan is a Medicare Advantage C-SNP prospect in CA and NV",
    "Kafka consumer lag spiking on member-match topic, partition 7",
    "We chose ArcadeDB for the unified graph plus vector store",
    "Sai is the assignee for all P2P scheduler bugs in this sprint",
    "Provider directory sync runs nightly at 02:00 UTC against PECOS",
]


@pytest.fixture
def seeded_ns(http, ns_hybrid: str) -> str:
    """Seed the fixed corpus into a unique namespace, return the ns."""
    for content in _CORPUS:
        r = http.post(
            "/api/v1/memory/",
            json={"content": content, "namespace": ns_hybrid, "memory_type": "fact"},
        )
        assert r.status_code == 201, f"seed failed: {r.status_code} {r.text}"
    return ns_hybrid


def _search(http, ns: str, q: str, mode: str, top_k: int = 3) -> list[dict]:
    r = http.get(
        "/api/v1/memory/search",
        params={"q": q, "ns": ns, "mode": mode, "top_k": top_k},
    )
    assert r.status_code == 200, f"GET /memory/search failed: {r.status_code} {r.text}"
    return r.json()


def _content_of(results: list[dict], index: int) -> str:
    assert len(results) > index, f"expected at least {index+1} results, got {len(results)}"
    return results[index]["content"]


# ---------------------------------------------------------------------------
# Vector mode — semantic retrieval works
# ---------------------------------------------------------------------------

class TestVectorMode:
    def test_vector_finds_semantic_match(self, http, seeded_ns: str):
        results = _search(http, seeded_ns, "database choice for analytics", mode="vector")
        assert results, "vector returned no results"
        # The top-3 must contain the DB decision somewhere.
        topk_contents = [r["content"] for r in results]
        assert any("MongoDB" in c and "PostgreSQL" in c for c in topk_contents), (
            f"vector failed to surface DB decision in top {len(results)}: {topk_contents}"
        )


# ---------------------------------------------------------------------------
# BM25 mode — exact-token retrieval works
# ---------------------------------------------------------------------------

class TestBM25Mode:
    def test_bm25_matches_exact_ticket_id(self, http, seeded_ns: str):
        results = _search(http, seeded_ns, "HPTE-413", mode="bm25")
        assert results, "bm25 returned no results"
        assert "HPTE-413" in results[0]["content"], (
            f"bm25 top hit doesn't contain the exact token: {results[0]['content']!r}"
        )

    def test_bm25_unknown_token_returns_empty_safely(self, http, seeded_ns: str):
        results = _search(http, seeded_ns, "xyzzy-no-such-token-anywhere", mode="bm25")
        # No exception, just no matches.
        assert isinstance(results, list)
        assert results == []


# ---------------------------------------------------------------------------
# RRF mode — the headline test
# ---------------------------------------------------------------------------

class TestRRFMode:
    def test_rrf_returns_results_for_simple_query(self, http, seeded_ns: str):
        results = _search(http, seeded_ns, "HPTE-413", mode="rrf")
        assert results, "rrf returned no results"
        assert "HPTE-413" in results[0]["content"], (
            f"rrf top hit doesn't contain exact token: {results[0]['content']!r}"
        )

    def test_rrf_picks_semantic_winner_over_lexical_noise(self, http, seeded_ns: str):
        """The key demo: query 'database choice for analytics'.

        - BM25 ranks the Innovaccer "population health analytics" line first
          (matches the word "analytics" literally).
        - Vector ranks the MongoDB→PostgreSQL OLAP decision first.
        - RRF fuses them — the DB decision wins because it's strong in both
          (high in vector, also present in BM25), beating the lexical-only
          Innovaccer hit.
        """
        rrf = _search(http, seeded_ns, "database choice for analytics", mode="rrf")
        assert rrf, "rrf returned no results"
        top = _content_of(rrf, 0)
        assert "MongoDB" in top and "PostgreSQL" in top, (
            f"RRF top result should be the DB decision; got {top!r}"
        )

    def test_rrf_score_scale_passes_router_floor(self, http, seeded_ns: str):
        """Regression for the bug we just fixed: the 0.45 score-floor in the
        router used to filter out every RRF result because RRF scores are
        ~0.03. The router must only apply the floor to cosine modes.
        """
        rrf = _search(http, seeded_ns, "scheduler bugs sprint", mode="rrf", top_k=3)
        assert rrf, "RRF returned empty — score-floor regression"
        for r in rrf:
            # RRF scores are guaranteed >0 by construction and < ~0.1 in practice
            assert 0.0 < r["score"] < 1.0, (
                f"unexpected RRF score scale: {r['score']!r} for {r['content'][:60]!r}"
            )
