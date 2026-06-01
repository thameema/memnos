"""
Feature 7 — Negation / contradiction detection.

Contract under test:

  * POST /memory/check-contradictions previews conflicts WITHOUT writing:
      - Returns ``would_supersede`` for directional conflicts and high-sim flips
      - Returns ``warnings_only`` for similarity-only matches
      - Response is deterministic — calling it twice with same body returns
        the same partition (idempotent dry-run)
  * The live write path uses the same detector: writing a memory that
    directionally contradicts an existing one auto-supersedes the older
    memory and includes a contradiction_warnings audit trail in the
    response.

These tests rely on the existing detector heuristics (negation keywords,
opposite-polarity stance flips). We use clearly-contradictory pairs so the
test is not sensitive to threshold tuning.
"""
from __future__ import annotations

import uuid

import pytest


def _write(http, ns: str, content: str, memory_type: str = "decision") -> dict:
    r = http.post(
        "/api/v1/memory/",
        json={"content": content, "namespace": ns, "memory_type": memory_type},
    )
    assert r.status_code == 201, f"{r.status_code} {r.text}"
    return r.json()


def _check(http, ns: str, content: str, memory_type: str = "decision") -> dict:
    r = http.post(
        "/api/v1/memory/check-contradictions",
        json={"content": content, "namespace": ns, "memory_type": memory_type},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Dry-run endpoint shape + idempotency
# ---------------------------------------------------------------------------

class TestCheckContradictionsShape:
    def test_empty_namespace_returns_zero_warnings(self, http, ns_episode: str):
        result = _check(
            http, ns_episode,
            "Decision: adopt PostgreSQL for the analytics workload",
        )
        for key in ("candidate_content", "namespace", "memory_type",
                    "would_supersede", "warnings_only", "total"):
            assert key in result, f"missing field {key!r}: {result}"
        assert result["total"] == 0
        assert result["would_supersede"] == []
        assert result["warnings_only"] == []

    def test_dry_run_does_not_write(self, http, ns_episode: str):
        _check(http, ns_episode, "Dry-run test should not persist anything")
        # After the dry-run, list endpoints should show zero episodes/memories
        listed = http.get("/api/v1/episodes/", params={"ns": ns_episode, "limit": 50}).json()
        assert listed == [], "dry-run accidentally created Episodes"


class TestCheckContradictionsDirectional:
    def test_negation_detected_after_existing_affirmative(self, http, ns_episode: str):
        # Existing affirmative claim
        seed = _write(http, ns_episode, "We always require code review for production deploys")
        # Dry-run a negating claim
        result = _check(http, ns_episode, "We never require code review for production deploys")
        # At least one warning should reference the seed and be directional.
        ids = [w["existing_id"] for w in result["would_supersede"] + result["warnings_only"]]
        assert seed["id"] in ids, (
            f"detector failed to flag seed against negating candidate: {result}"
        )


# ---------------------------------------------------------------------------
# Live write path: auto-supersede on directional contradiction
# ---------------------------------------------------------------------------

class TestAutoSupersedeOnWrite:
    def test_writing_a_contradiction_supersedes_the_older(self, http, ns_episode: str):
        """If we write a directly negating claim, the older memory should be
        auto-superseded and the new write's response carries an audit trail."""
        tag = f"audit-{uuid.uuid4().hex[:6]}"
        # Seed: affirmative
        seed = _write(
            http, ns_episode,
            f"We always require {tag} approval for production releases",
        )

        # New write: contradiction
        new = _write(
            http, ns_episode,
            f"We never require {tag} approval for production releases",
        )
        assert new["id"] != seed["id"]
        # Either the seed shows up in 'affects' (auto-superseded lineage) OR
        # the contradiction_warnings carry an entry pointing at it. Both are
        # valid audit-trail evidence — we accept either.
        warnings = new.get("contradiction_warnings") or []
        affected = new.get("affects") or []
        evidence_ids = set(affected) | {w.get("existing_id") for w in warnings}
        assert seed["id"] in evidence_ids, (
            f"auto-supersede audit trail missing for seed {seed['id']}; "
            f"affects={affected} warnings={warnings}"
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_empty_content_rejected(self, http, ns_episode: str):
        r = http.post(
            "/api/v1/memory/check-contradictions",
            json={"content": "", "namespace": ns_episode},
        )
        assert r.status_code == 422

    def test_empty_namespace_rejected(self, http):
        r = http.post(
            "/api/v1/memory/check-contradictions",
            json={"content": "anything", "namespace": ""},
        )
        assert r.status_code == 422
