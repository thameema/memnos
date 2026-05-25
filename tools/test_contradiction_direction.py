"""
tools/test_contradiction_direction.py — Unit tests for contradiction direction detection.

Tests cover:
- _negated_phrases()      — extracts negated object phrases
- _affirmed_phrases()     — extracts affirmed object phrases
- _dominant_stance()      — affirmative / negative / neutral classification
- detect_direction()      — negation_detected / opposite_polarity / None
- check_contradictions()  — direction field in ContradictionWarning
"""
from __future__ import annotations

import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, _REPO_ROOT + "/packages/core")

from engram.contradiction.detector import (
    ContradictionWarning,
    _affirmed_phrases,
    _dominant_stance,
    _negated_phrases,
    check_contradictions,
    detect_direction,
)


# ---------------------------------------------------------------------------
# _negated_phrases
# ---------------------------------------------------------------------------

class TestNegatedPhrases(unittest.TestCase):
    def test_dont_use(self):
        phrases = _negated_phrases("Don't use PostgreSQL for analytics workloads")
        self.assertTrue(any("postgresql" in p for p in phrases))

    def test_avoid(self):
        phrases = _negated_phrases("Avoid raw SQL queries in service code")
        self.assertTrue(any("raw" in p or "sql" in p for p in phrases))

    def test_deprecated(self):
        phrases = _negated_phrases("The legacy API is deprecated in favour of REST")
        self.assertTrue(any("legacy" in p or "api" in p for p in phrases))

    def test_no_match_plain_sentence(self):
        phrases = _negated_phrases("We use PostgreSQL for all transactional data")
        self.assertEqual(len(phrases), 0)

    def test_do_not(self):
        phrases = _negated_phrases("Do not deploy on bare metal")
        self.assertTrue(any("deploy" in p or "bare" in p for p in phrases))


# ---------------------------------------------------------------------------
# _affirmed_phrases
# ---------------------------------------------------------------------------

class TestAffirmedPhrases(unittest.TestCase):
    def test_use(self):
        phrases = _affirmed_phrases("Use PostgreSQL for all transactional data")
        self.assertTrue(any("postgresql" in p for p in phrases))

    def test_prefer(self):
        phrases = _affirmed_phrases("Prefer Redis for session caching")
        self.assertTrue(any("redis" in p for p in phrases))

    def test_deploy(self):
        phrases = _affirmed_phrases("Deploy all services on Kubernetes")
        self.assertTrue(any("all" in p or "services" in p for p in phrases))

    def test_no_match(self):
        phrases = _affirmed_phrases("PostgreSQL is deprecated, avoid it")
        self.assertEqual(len(phrases), 0)


# ---------------------------------------------------------------------------
# _dominant_stance
# ---------------------------------------------------------------------------

class TestDominantStance(unittest.TestCase):
    def test_affirmative_sentence(self):
        self.assertEqual(_dominant_stance("Use Postgres, always prefer it"), "affirmative")

    def test_negative_sentence(self):
        self.assertEqual(_dominant_stance("Avoid raw SQL, never use it in services"), "negative")

    def test_neutral_sentence(self):
        self.assertEqual(_dominant_stance("The database runs on port 5432"), "neutral")

    def test_explicit_negation_tips_to_negative(self):
        self.assertEqual(
            _dominant_stance("The old library is deprecated and we must stop using it"),
            "negative",
        )


# ---------------------------------------------------------------------------
# detect_direction
# ---------------------------------------------------------------------------

class TestDetectDirection(unittest.TestCase):
    def test_explicit_negation_of_affirmed_subject(self):
        new_text = "Do not use Redis for persistent storage"
        existing = "Use Redis for session state storage"
        direction = detect_direction(new_text, existing)
        self.assertEqual(direction, "negation_detected")

    def test_reverse_negation(self):
        existing = "Never use raw SQL in service code"
        new_text = "Use raw SQL for complex analytics queries in the reporting service"
        direction = detect_direction(new_text, existing)
        self.assertEqual(direction, "negation_detected")

    def test_opposite_polarity(self):
        new_text = "Avoid AWS for all cloud deployments; prefer Azure"
        existing = "Use AWS, it should be the primary cloud provider"
        direction = detect_direction(new_text, existing)
        self.assertIn(direction, ("negation_detected", "opposite_polarity"))

    def test_no_contradiction_similar_topic(self):
        new_text = "Use PostgreSQL 16 for read replicas"
        existing = "Use PostgreSQL 15 for the primary instance"
        # Both affirmative, same subject — not contradictory
        direction = detect_direction(new_text, existing)
        # Should be None or opposite_polarity (both affirmative so NOT opposite)
        self.assertNotEqual(direction, "negation_detected")

    def test_unrelated_texts(self):
        new_text = "The team standup is at 10am every Monday"
        existing = "Quarterly planning happens in January"
        direction = detect_direction(new_text, existing)
        self.assertIsNone(direction)

    def test_explicit_deprecated_vs_use(self):
        new_text = "XMLHttpRequest is deprecated — use fetch instead"
        existing = "Use XMLHttpRequest for all AJAX calls in the frontend"
        direction = detect_direction(new_text, existing)
        self.assertIn(direction, ("negation_detected", "opposite_polarity"))


# ---------------------------------------------------------------------------
# check_contradictions — direction field in ContradictionWarning
# ---------------------------------------------------------------------------

def _make_mock_client(search_results):
    client = MagicMock()
    client.search = AsyncMock(return_value=search_results)
    return client


def _make_search_result(content: str, score: float):
    mem = MagicMock()
    mem.id = "existing-001"
    mem.content = content
    result = MagicMock()
    result.score = score
    result.memory = mem
    return result


class TestCheckContradictions(unittest.IsolatedAsyncioTestCase):
    async def test_high_similarity_returns_warning(self):
        existing_content = "Use PostgreSQL for all transactional databases"
        new_content = "Do not use PostgreSQL, switch to MySQL instead"
        results = [_make_search_result(existing_content, score=0.95)]
        client = _make_mock_client(results)
        warnings = await check_contradictions(client, new_content, "test:ns")
        self.assertEqual(len(warnings), 1)
        self.assertGreater(warnings[0].similarity, 0.88)

    async def test_direction_field_set_on_warning(self):
        existing_content = "Use Redis for caching"
        new_content = "Avoid Redis; use Memcached instead for caching"
        results = [_make_search_result(existing_content, score=0.92)]
        client = _make_mock_client(results)
        warnings = await check_contradictions(client, new_content, "test:ns")
        if warnings:
            self.assertIn(warnings[0].direction, ("negation_detected", "opposite_polarity", "similarity_only"))
            self.assertNotEqual(warnings[0].direction, "")

    async def test_low_similarity_no_warning(self):
        results = [_make_search_result("Use Redis for caching", score=0.50)]
        client = _make_mock_client(results)
        warnings = await check_contradictions(client, "Anything unrelated", "test:ns")
        self.assertEqual(len(warnings), 0)

    async def test_same_opening_skipped(self):
        # Same first sentence → likely an update, not a contradiction
        existing_content = "Use PostgreSQL for production databases. Consider read replicas."
        new_content = "Use PostgreSQL for production databases. Version 16 is recommended."
        results = [_make_search_result(existing_content, score=0.95)]
        client = _make_mock_client(results)
        warnings = await check_contradictions(client, new_content, "test:ns")
        self.assertEqual(len(warnings), 0)

    async def test_search_failure_returns_empty(self):
        client = MagicMock()
        client.search = AsyncMock(side_effect=RuntimeError("DB error"))
        warnings = await check_contradictions(client, "anything", "test:ns")
        self.assertEqual(warnings, [])

    async def test_warning_has_direction_attribute(self):
        w = ContradictionWarning(existing_id="x", existing_content="y", similarity=0.9,
                                  reason="test", direction="negation_detected")
        self.assertEqual(w.direction, "negation_detected")

    async def test_negation_detected_direction(self):
        existing_content = "Deploy all services on AWS"
        new_content = "Do not deploy on AWS; use Azure for all services"
        results = [_make_search_result(existing_content, score=0.93)]
        client = _make_mock_client(results)
        warnings = await check_contradictions(client, new_content, "test:ns")
        if warnings:
            self.assertIn(warnings[0].direction, ("negation_detected", "opposite_polarity"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
