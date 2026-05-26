"""
engram.contradiction.detector — Detects semantic contradictions before memory writes.

Non-blocking: contradiction checks run async and return warnings. They never
prevent a write from completing. The check uses three layers:

Layer 1 — Vector similarity: flag high-similarity pairs above the threshold.
Layer 2 — Direction detection (heuristics, free):
    - negation_detected   : new text negates something affirmed in existing
    - opposite_polarity   : same subject, opposite stance (use X vs. avoid X)
    - topic_update        : same opening phrase, divergent completion (status flip)
    - similarity_only     : high similarity but no heuristic direction found
Layer 3 — LLM arbitration (for ambiguous similarity_only at 0.65–0.88):
    - llm_confirmed       : LLM says YES — new memory supersedes existing
    - Warnings dropped    : LLM says NO — confirmed not a contradiction

Threshold strategy
------------------
- decision / fact / incident  → 0.65  (catch planning-vs-completion, status flips)
- all other types             → 0.88  (avoid noise from session logs, commits)

LLM model
---------
Uses ANTHROPIC_API_KEY (Haiku) or OPENAI_API_KEY (gpt-4o-mini) from environment.
Override model with ENGRAM_CONTRADICTION_MODEL env var.
If no key is available, Layer 3 is skipped and similarity_only warnings surface as-is.

Noise filtering
---------------
Existing memories tagged session-log, auto, or git-commit are skipped as
contradiction sources — those are ephemeral observations, not claims.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.client import EngramClient

logger = logging.getLogger(__name__)

# Broad threshold: important memory types where planning-vs-completion matters
_SIMILARITY_THRESHOLD_BROAD = 0.65
_IMPORTANT_TYPES = frozenset(["decision", "fact", "incident"])

# Strict threshold: other types (session logs, commits) — only near-exact duplicates
_SIMILARITY_THRESHOLD_STRICT = 0.88

# Tags that mark auto-generated, ephemeral memories — skip as contradiction sources
_NOISE_TAGS = frozenset(["session-log", "auto", "git-commit"])

_MAX_CANDIDATES = 8

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


# ---------------------------------------------------------------------------
# Layer 3 — LLM arbitration
# ---------------------------------------------------------------------------

_LLM_PROMPT = """Memory A (existing):
{existing}

Memory B (new):
{new}

Does Memory B make Memory A false, outdated, or superseded?
Answer YES or NO only."""

_LLM_TIMEOUT_S = 5.0
_LLM_MAX_TOKENS = 10


async def _llm_check_contradiction(new_content: str, existing_content: str) -> bool | None:
    """Ask the configured LLM whether new_content contradicts existing_content.

    Returns:
        True  — LLM confirmed contradiction (caller should auto-supersede)
        False — LLM confirmed no contradiction (caller should drop warning)
        None  — LLM unavailable or timed out (caller falls back to warn-only)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    provider = "anthropic" if api_key else None
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            provider = "openai"

    if not provider or not api_key:
        return None

    prompt = _LLM_PROMPT.format(
        existing=existing_content[:600],
        new=new_content[:600],
    )

    try:
        if provider == "anthropic":
            import anthropic  # type: ignore
            model = os.environ.get("ENGRAM_CONTRADICTION_MODEL", "claude-haiku-4-5-20251001")
            aclient = anthropic.AsyncAnthropic(api_key=api_key)
            response = await asyncio.wait_for(
                aclient.messages.create(
                    model=model,
                    max_tokens=_LLM_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=_LLM_TIMEOUT_S,
            )
            answer = response.content[0].text.strip().upper()

        else:  # openai
            from openai import AsyncOpenAI  # type: ignore
            model = os.environ.get("ENGRAM_CONTRADICTION_MODEL", "gpt-4o-mini")
            oclient = AsyncOpenAI(api_key=api_key)
            response = await asyncio.wait_for(
                oclient.chat.completions.create(
                    model=model,
                    max_tokens=_LLM_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=_LLM_TIMEOUT_S,
            )
            answer = (response.choices[0].message.content or "").strip().upper()

        result = answer.startswith("YES")
        logger.debug("llm_contradiction_check | result=%s answer=%r", result, answer[:20])
        return result

    except asyncio.TimeoutError:
        logger.debug("LLM contradiction check timed out after %.1fs", _LLM_TIMEOUT_S)
        return None
    except Exception as exc:
        logger.debug("LLM contradiction check failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Layer 2 — Direction heuristics
# ---------------------------------------------------------------------------

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
    - "topic_update"       : same opening phrase, divergent completion (status flip)
    - None                 : no directional contradiction found (still may be similarity-only)
    """
    new_neg      = _negated_phrases(new_content)
    existing_aff = _affirmed_phrases(existing_content)
    existing_neg = _negated_phrases(existing_content)
    new_aff      = _affirmed_phrases(new_content)

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
        new_words = set(_normalize(new_content).split())
        ex_words  = set(_normalize(existing_content).split())
        stopwords = {"a", "an", "the", "is", "are", "it", "to", "for", "in", "on", "with", "and", "or", "that", "this"}
        shared = (new_words & ex_words) - stopwords
        if len(shared) >= 2:
            return "opposite_polarity"

    # Topic update: same opening phrase, divergent completion.
    # Pattern: "X: not yet done. Planned..." → "X: completed. Running..."
    # Strip punctuation from word comparison so "backend:" matches "backend".
    new_words_list  = [w.rstrip(",:;.") for w in _normalize(new_content).split()]
    ex_words_list   = [w.rstrip(",:;.") for w in _normalize(existing_content).split()]
    shared_prefix = 0
    for a, b in zip(new_words_list, ex_words_list):
        if a == b:
            shared_prefix += 1
        else:
            break
    # Require at least 3 shared opening words AND content diverges after the prefix
    if shared_prefix >= 3 and new_words_list[shared_prefix:] != ex_words_list[shared_prefix:]:
        return "topic_update"

    return None


@dataclass
class ContradictionWarning:
    existing_id: str
    existing_content: str
    similarity: float
    reason: str = ""
    direction: str = ""
    # direction values:
    #   negation_detected  — heuristic: explicit negation keyword
    #   opposite_polarity  — heuristic: stance flip on same subject
    #   topic_update       — heuristic: same prefix, divergent completion
    #   llm_confirmed      — Layer 3: LLM said YES (triggers auto-supersede)
    #   similarity_only    — ambiguous: LLM unavailable, surface as warning


async def check_contradictions(
    client: "EngramClient",
    content: str,
    namespace: str,
    memory_type: str = "fact",
    tags: list[str] | None = None,
) -> list[ContradictionWarning]:
    """
    Search for existing memories that are highly similar to *content*.

    Returns a list of ContradictionWarning — one per candidate that exceeds
    the applicable similarity threshold. Empty list means no contradictions detected.

    Args:
        memory_type: type of the *new* memory being written. Controls the threshold:
            decision/fact/incident → 0.65 (catches planning-vs-completion flips).
            all others             → 0.88 (only near-exact duplicates).
        tags: tags on the *new* memory. Writes tagged session-log / auto / git-commit
            skip contradiction detection entirely — they are observations, not claims.

    Each warning includes a ``direction`` field:
    - "negation_detected"  : heuristic — explicit negation keyword found
    - "opposite_polarity"  : heuristic — stance flip on same subject
    - "topic_update"       : heuristic — same opening phrase, divergent completion
    - "llm_confirmed"      : LLM confirmed the contradiction (triggers auto-supersede)
    - "similarity_only"    : ambiguous — LLM unavailable, surface for human review
    """
    # Skip entirely if this write is itself ephemeral/auto-generated
    if tags and set(tags) & _NOISE_TAGS:
        return []

    threshold = (
        _SIMILARITY_THRESHOLD_BROAD
        if memory_type.lower() in _IMPORTANT_TYPES
        else _SIMILARITY_THRESHOLD_STRICT
    )

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
        # Pinned governance records have score=2.0 (an artificial governance weight,
        # not a cosine similarity). Skip them — contradiction detection operates on
        # semantic similarity only, not on entity-matching governance signals.
        if score > 1.0:
            continue
        if score < threshold:
            continue
        existing = r.memory if hasattr(r, "memory") else r
        existing_content = str(getattr(existing, "content", ""))

        # Skip ephemeral auto-generated memories — they are observations, not claims
        existing_tags = set(getattr(existing, "tags", None) or [])
        if existing_tags & _NOISE_TAGS:
            continue

        # Skip self-match: first sentence of existing == first sentence of new
        new_first = content.split(".")[0].lower().strip()
        exist_first = existing_content.split(".")[0].lower().strip()
        if new_first == exist_first[: len(new_first)]:
            continue

        direction = detect_direction(content, existing_content) or "similarity_only"

        # For the broad threshold range (0.65–0.88): heuristics found no direction.
        # Gate with shared subject overlap first (cheap), then LLM arbitration.
        if direction == "similarity_only" and score < _SIMILARITY_THRESHOLD_STRICT:
            stopwords = {"a","an","the","is","are","it","to","for","in","on",
                         "with","and","or","that","this","was","has","have","been",
                         "not","yet","done","will","be"}
            new_words = set(content.lower().split()) - stopwords
            ex_words  = set(existing_content.lower().split()) - stopwords
            if len(new_words & ex_words) < 3:
                continue  # unrelated topics — skip before paying LLM cost

            # Layer 3: LLM arbitration for ambiguous same-topic pairs
            llm_result = await _llm_check_contradiction(content, existing_content)
            if llm_result is True:
                direction = "llm_confirmed"    # auto-supersede in router
            elif llm_result is False:
                continue                        # LLM confirmed not a contradiction
            # llm_result is None → LLM unavailable, keep similarity_only as warning

        if direction == "negation_detected":
            reason = f"Explicit negation detected (similarity {score:.2f})"
        elif direction == "opposite_polarity":
            reason = f"Opposite stance on same subject (similarity {score:.2f})"
        elif direction == "topic_update":
            reason = f"Status update on same topic (similarity {score:.2f})"
        elif direction == "llm_confirmed":
            reason = f"LLM confirmed contradiction (similarity {score:.2f})"
        else:
            reason = f"High similarity ({score:.2f}) — LLM unavailable, review manually"

        warnings.append(ContradictionWarning(
            existing_id=str(getattr(existing, "id", "")),
            existing_content=existing_content[:300],
            similarity=score,
            reason=reason,
            direction=direction,
        ))

    logger.debug(
        "contradiction_check | type=%s threshold=%.2f candidates=%d warnings=%d",
        memory_type, threshold, len(results or []), len(warnings),
    )
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
