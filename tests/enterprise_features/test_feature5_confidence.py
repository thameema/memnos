"""
Feature 5 — Confidence scoring on every memory.

Contract under test:

  * Every Memory carries ``confidence`` in [0.0, 1.0].
  * If the caller omits it, the server picks a per-source default:
      api      → 1.00
      user     → 1.00
      file     → 0.85  (ingest)
      webhook  → 0.80
      voice    → 0.75
      agent    → 0.70
  * Confidence round-trips through POST → GET → search response.
  * GET /memory/search?min_confidence=X drops results below the threshold.
  * Ingested file chunks land at the 'file' default (0.85), so a
    min_confidence=0.95 filter excludes them.
  * Rejected: confidence outside [0.0, 1.0] returns 422.
"""
from __future__ import annotations

import uuid


def _write(http, ns: str, content: str, **extra) -> dict:
    body = {"content": content, "namespace": ns, "memory_type": "fact"}
    body.update(extra)
    r = http.post("/api/v1/memory/", json=body)
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    return r.json()


class TestConfidenceDefaults:
    def test_default_for_api_source_is_one(self, http, ns_episode: str):
        # source omitted → default 'user' on request schema → conf 1.0
        mem = _write(http, ns_episode, "explicit user fact")
        assert mem["confidence"] == 1.0

    def test_explicit_confidence_overrides_default(self, http, ns_episode: str):
        mem = _write(http, ns_episode, "weak inference", confidence=0.4)
        assert abs(mem["confidence"] - 0.4) < 1e-6


class TestConfidenceRoundtrip:
    def test_get_memory_returns_confidence(self, http, ns_episode: str):
        mem = _write(http, ns_episode, "fact about widgets", confidence=0.6)
        got = http.get(f"/api/v1/memory/{mem['id']}", params={"ns": ns_episode}).json()
        assert abs(got["confidence"] - 0.6) < 1e-6

    def test_search_results_carry_confidence(self, http, ns_episode: str):
        mem = _write(http, ns_episode, "decisive widget colour is blue", confidence=0.92)
        r = http.get(
            "/api/v1/memory/search",
            params={"q": "widget colour blue", "ns": ns_episode, "mode": "vector", "top_k": 3},
        )
        results = r.json()
        match = next((x for x in results if x["id"] == mem["id"]), None)
        assert match is not None, "seed memory missing from search results"
        assert abs(match["confidence"] - 0.92) < 1e-6


class TestMinConfidenceFilter:
    def test_min_confidence_drops_below_threshold(self, http, ns_episode: str):
        # Use BM25 mode to bypass the cosine 0.45 floor. Use semantically
        # *distinct* content sharing only a unique token, so contradiction
        # detection doesn't auto-supersede one seed with the other — this
        # test is about the confidence filter, not contradiction handling.
        tag = f"corrtag-{uuid.uuid4().hex[:6]}"
        high = _write(http, ns_episode, f"quarterly revenue forecast {tag} alpha branch", confidence=0.95)
        low  = _write(http, ns_episode, f"upstream vendor onboarding {tag} bravo branch", confidence=0.40)

        # No filter — both surface via the shared unique token
        unfiltered = http.get(
            "/api/v1/memory/search",
            params={"q": tag, "ns": ns_episode, "mode": "bm25", "top_k": 5},
        ).json()
        ids_unfiltered = {r["id"] for r in unfiltered}
        assert {high["id"], low["id"]} <= ids_unfiltered, (
            f"both seeds should round-trip via BM25 search; got {ids_unfiltered}"
        )

        # With filter — low confidence is dropped
        filtered = http.get(
            "/api/v1/memory/search",
            params={
                "q": tag, "ns": ns_episode,
                "mode": "bm25", "top_k": 5, "min_confidence": 0.90,
            },
        ).json()
        ids_filtered = {r["id"] for r in filtered}
        assert high["id"] in ids_filtered, "high-confidence memory was dropped"
        assert low["id"] not in ids_filtered, (
            f"low-confidence memory survived min_confidence=0.90 filter: {ids_filtered}"
        )


class TestIngestSourceDefault:
    """Memories created via /ingest/file get the 'file' default = 0.85."""

    def test_file_chunks_default_to_0_85(self, http, ns_episode: str):
        import io
        body = (
            b"# Memo\n\nFile-derived facts may contain OCR or formatting "
            b"artefacts, so the memnos service tags them at the 'file' "
            b"confidence default of 0.85 by policy."
        )
        r = http.post(
            "/api/v1/ingest/file",
            data={"namespace": ns_episode},
            files={"file": ("memo.md", io.BytesIO(body), "text/markdown")},
        )
        assert r.status_code == 201, f"{r.status_code} {r.text}"
        memory_ids = r.json()["memory_ids"]
        assert memory_ids, "ingest produced zero memories"

        for mid in memory_ids:
            got = http.get(f"/api/v1/memory/{mid}", params={"ns": ns_episode}).json()
            assert abs(got["confidence"] - 0.85) < 1e-6, (
                f"ingested chunk {mid} has confidence={got['confidence']}, expected 0.85"
            )

    def test_min_confidence_0_9_excludes_ingested_chunks(self, http, ns_episode: str):
        import io
        body = (
            b"# Important Memo\n\nThe quarterly compliance review found "
            b"no critical issues remaining at the end of Q3 2026."
        )
        r = http.post(
            "/api/v1/ingest/file",
            data={"namespace": ns_episode},
            files={"file": ("compliance.md", io.BytesIO(body), "text/markdown")},
        )
        assert r.status_code == 201
        memory_ids = set(r.json()["memory_ids"])

        # min_confidence=0.9 excludes file-sourced chunks (which are 0.85)
        filtered = http.get(
            "/api/v1/memory/search",
            params={
                "q": "quarterly compliance review",
                "ns": ns_episode,
                "mode": "vector",
                "top_k": 5,
                "min_confidence": 0.9,
            },
        ).json()
        surviving = {r["id"] for r in filtered}
        assert not (memory_ids & surviving), (
            f"ingest chunks survived min_confidence=0.9 filter: {memory_ids & surviving}"
        )


class TestValidation:
    def test_negative_confidence_rejected(self, http, ns_episode: str):
        r = http.post(
            "/api/v1/memory/",
            json={
                "content": "bad confidence", "namespace": ns_episode,
                "memory_type": "fact", "confidence": -0.1,
            },
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_confidence_over_one_rejected(self, http, ns_episode: str):
        r = http.post(
            "/api/v1/memory/",
            json={
                "content": "bad confidence", "namespace": ns_episode,
                "memory_type": "fact", "confidence": 1.5,
            },
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
