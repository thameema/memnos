"""
engram.contradiction.detector — Detects semantic contradictions before memory writes.

Non-blocking: contradiction checks run async and return warnings. They never
prevent a write from completing. The check uses two layers:

Layer 1 — Vector similarity: flag high-similarity pairs (cosine > 0.88).
Layer 2 — Direction detection: classify *how* they contradict:
    - negation_detected   : new text contains explicit negation of existing claim
    - opposite_polarity   : same subject, opposite stance (use X vs. avoid X)
    - similarity_only     : high similarity but no explicit negation found
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.client import EngramClient

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.88   # flag if cosine similarity > this
_MAX_CANDIDATES = 5

# Affirmative stance keywords
_AFFIRMATIVE = frozenset([
    "use", "adopt", "prefer", "require", "always", "should", "must",
    "enable", "deploy", "apply", "implement", "enforce", "choose", "ensure",
])
# Negative stance keywords
_NEGATIVE = frozenset([
    "avoid", "never", "deprecated", "prohibited", "forbidden", "disallow",
    "remove", "stop", "disable", "drop", "discontinue", "reject",
])

# Negation patterns — words that flip a following phrase's meaning
_NEGATION_PATTERN = re.compile(
    r"\b(don'?t|do not|not|no longer|avoid|never|stop using|stop|prohibited"
    r"|instead of|rather than|disable|disallow|forbid|removed|dropped)\s+"
    r"([a-z][a-z0-9_\-]{1,30}(?:\s+[a-z][a-z0-9_\-]{0,20}){0,3})",
    re.IGNORECASE,
)

# Passive deprecated: "X is deprecated", "X was deprecated"
_PASSIVE_DEPRECATED = re.compile(
    r"\b([a-z][a-z0-9_\-]{1,30})\s+(?:is|was|has been|have been|are)\s+deprecated",
    re.IGNORECASE,
)

# Affirmative-use patterns: "use X", "prefer X", "should use X"
_AFFIRMATIVE_PATTERN = re.compile(
    r"\b(use|adopt|prefer|enable|deploy|implement|require)\s+"
    r"([a-z][a-z0-9_\-]{1,30}(?:\s+[a-z][a-z0-9_\-]{0,20}){0,2})",
    re.IGNORECASE,
)

# Words that are themselves verbs/affirmative operators (strip from negated phrase head)
_AFF_VERBS = frozenset(["use", "adopt", "prefer", "enable", "deploy", "implement",
                         "require", "apply", "install", "run", "include", "add"])


def _normalize(text: str) -> str:
    return text.lower().strip()


def _negated_phrases(text: str) -> set[str]:
    """Extract the object phrases that are negated in *text*."""
    results: set[str] = set()

    for m in _NEGATION_PATTERN.finditer(text):
        phrase = _normalize(m.group(2))
        results.add(phrase)
        words = phrase.split()
        # Add first word
        results.add(words[0])
        # If the phrase starts with an affirmative verb, strip it and add the rest
        if words[0] in _AFF_VERBS and len(words) > 1:
            rest = " ".join(words[1:])
            results.add(rest)
            results.add(words[1])  # first content word after the verb

    for m in _PASSIVE_DEPRECATED.finditer(text):
        subject = _normalize(m.group(1))
        results.add(subject)

    return results


def _affirmed_phrases(text: str) -> set[str]:
    """Extract the object phrases that are positively affirmed in *text*."""
    results: set[str] = set()
    for m in _AFFIRMATIVE_PATTERN.finditer(text):
        phrase = _normalize(m.group(2))
        results.add(phrase)
        results.add(phrase.split()[0])
    return results


def _dominant_stance(text: str) -> str:
    """Return 'affirmative', 'negative', or 'neutral' based on keyword balance."""
    words = set(_normalize(text).split())
    aff_count = sum(1 for w in _AFFIRMATIVE if w in words)
    neg_count = sum(1 for w in _NEGATIVE if w in words)
    # Also count negation pattern matches
    neg_count += len(_negated_phrases(text))
    if neg_count > aff_count:
        return "negative"
    if aff_count > neg_count:
        return "affirmative"
    return "neutral"


def detect_direction(new_content: str, existing_content: str) -> str | None:
    """
    Analyse whether *new_content* directly contradicts *existing_content*.

    Returns one of:
    - "negation_detected"  : new text negates something affirmed in existing (or vice versa)
    - "opposite_polarity"  : same subject, opposite stance (use X vs. avoid X)
    - None                 : no directional contradiction found (still may be similarity-only)
    """
    new_neg     = _negated_phrases(new_content)
    existing_aff = _affirmed_phrases(existing_content)
    existing_neg = _negated_phrases(existing_content)
    new_aff     = _affirmed_phrases(new_content)

    # New negates something existing affirms
    if new_neg & existing_aff:
        return "negation_detected"
    # Existing negates something new affirms
    if existing_neg & new_aff:
        return "negation_detected"

    # Opposite polarity: same subject, different stance words
    new_stance      = _dominant_stance(new_content)
    existing_stance = _dominant_stance(existing_content)
    if new_stance != "neutral" and existing_stance != "neutral" and new_stance != existing_stance:
        # Confirm subject overlap (shared noun/entity words > 2)
        new_words = set(_normalize(new_content).split())
        ex_words  = set(_normalize(existing_content).split())
        stopwords = {"a", "an", "the", "is", "are", "it", "to", "for", "in", "on", "with", "and", "or", "that", "this"}
        shared = (new_words & ex_words) - stopwords
        if len(shared) >= 2:
            return "opposite_polarity"

    return None


@dataclass
class ContradictionWarning:
    existing_id: str
    existing_content: str
    similarity: float
    reason: str = ""
    direction: str = ""   # "" | "negation_detected" | "opposite_polarity" | "similarity_only"


async def check_contradictions(
    client: "EngramClient",
    content: str,
    namespace: str,
) -> list[ContradictionWarning]:
    """
    Search for existing memories that are highly similar to *content*.

    Returns a list of ContradictionWarning — one per candidate that exceeds
    the similarity threshold. Empty list means no contradictions detected.

    Each warning now includes a ``direction`` field:
    - "negation_detected"  : explicit negation relationship found
    - "opposite_polarity"  : same subject, opposing stance
    - "similarity_only"    : high similarity with no detected direction
    """
    try:
        results = await client.search(
            query=content,
            namespace=namespace,
            top_k=_MAX_CANDIDATES,
            mode="vector",
        )
    except Exception as exc:
        logger.debug("Contradiction check search failed (non-fatal): %s", exc)
        return []

    warnings: list[ContradictionWarning] = []
    for r in results:
        score = float(getattr(r, "score", 0.0))
        if score < _SIMILARITY_THRESHOLD:
            continue
        existing = r.memory if hasattr(r, "memory") else r
        existing_content = str(getattr(existing, "content", ""))

        # Simple divergence heuristic: similar score but different first sentence
        new_first = content.split(".")[0].lower().strip()
        exist_first = existing_content.split(".")[0].lower().strip()
        if new_first == exist_first[:len(new_first)]:
            # Likely an update, not a contradiction — skip
            continue

        direction = detect_direction(content, existing_content) or "similarity_only"

        if direction == "negation_detected":
            reason = f"Explicit negation detected (similarity {score:.2f})"
        elif direction == "opposite_polarity":
            reason = f"Opposite stance on same subject (similarity {score:.2f})"
        else:
            reason = f"High similarity ({score:.2f}) with different opening claim"

        warnings.append(ContradictionWarning(
            existing_id=str(getattr(existing, "id", "")),
            existing_content=existing_content[:300],
            similarity=score,
            reason=reason,
            direction=direction,
        ))
    return warnings


class ContradictionDetector:
    """Stateless wrapper for use from client and MCP server."""

    async def check(
        self,
        client: "EngramClient",
        content: str,
        namespace: str,
    ) -> list[ContradictionWarning]:
        return await check_contradictions(client, content, namespace)
