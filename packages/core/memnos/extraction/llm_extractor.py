"""
memnos.extraction.llm_extractor — LLM-enriched typed relationship extraction.

Runs asynchronously after a memory write (fire-and-forget) to extract
typed, directional edges between entities that spaCy's MENTIONS edges
cannot capture.

Edge vocabulary
---------------
  CHOSE          — "We decided to use FHIR R4"
  PROHIBITS      — "Avoid Redis for session storage"
  WANTS          — "Centene wants SaaS delivery"
  DEADLINE       — time constraint: entity → date/milestone entity
  CAUSES         — "Redis outages caused downtime"
  DEPENDS_ON     — technical or logical dependency
  REPLACES       — supersedes/migrates away from something
  GOVERNS        — decision or constraint applies to an entity
  RATIONALE_FOR  — reason / justification for a decision
  RELATES_TO     — catch-all for relationships that don't fit above

Providers
---------
  anthropic   — uses ANTHROPIC_API_KEY (or config api_key)
  openai      — uses OPENAI_API_KEY (or config api_key + base_url)

The extractor tries the configured provider first, then auto-detects from
available environment variables. If no API key is available it logs a debug
message and returns an empty list without raising.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memnos.config import LLMExtractionConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge vocabulary
# ---------------------------------------------------------------------------

EDGE_VOCABULARY: set[str] = {
    "CHOSE",
    "PROHIBITS",
    "WANTS",
    "DEADLINE",
    "CAUSES",
    "DEPENDS_ON",
    "REPLACES",
    "GOVERNS",
    "RATIONALE_FOR",
    "RELATES_TO",
}

_CATCHALL_EDGE = "RELATES_TO"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ExtractedRelationship:
    """A single typed, directional relationship extracted by the LLM."""
    source: str           # source entity name (normalised to lowercase)
    edge_type: str        # one of EDGE_VOCABULARY
    target: str           # target entity name (normalised to lowercase)
    confidence: float = 0.8


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a precise relationship extractor. "
    "Given a piece of text, extract directional relationships between named entities "
    "using ONLY the allowed edge types. "
    "Respond with valid JSON only — no markdown, no explanation."
)

_USER_TMPL = """\
Extract relationships from the following text.

ALLOWED EDGE TYPES (use exactly these strings):
  CHOSE        — decided to use / adopt something
  PROHIBITS    — explicitly forbidden or not recommended
  WANTS        — desired outcome, feature, or requirement
  DEADLINE     — time constraint on an entity (target = date or milestone)
  CAUSES       — one thing causes another
  DEPENDS_ON   — technical or logical dependency
  REPLACES     — supersedes or migrates away from something
  GOVERNS      — decision / constraint / rule applies to an entity
  RATIONALE_FOR — reason or justification for a decision
  RELATES_TO   — use only when no other type fits

Rules:
- source and target must be named entities from the text (lowercase, max 5 words)
- confidence: 0.0–1.0 reflecting how clearly the relationship is stated
- omit relationships with confidence < 0.5
- return at most 10 relationships
- if no clear relationships exist, return {{"relationships": []}}

TEXT:
{content}

Respond with JSON only:
{{
  "relationships": [
    {{"source": "entity a", "edge_type": "CHOSE", "target": "entity b", "confidence": 0.95}},
    ...
  ]
}}
"""

# ---------------------------------------------------------------------------
# LLMExtractor
# ---------------------------------------------------------------------------

class LLMExtractor:
    """Async LLM client for typed relationship extraction.

    Instantiated with a :class:`LLMExtractionConfig`. If the configured
    provider/key is unavailable, all extraction calls return ``[]`` silently.
    """

    def __init__(self, config: "LLMExtractionConfig") -> None:
        self._config = config
        self._provider: str | None = None
        self._api_key: str | None = None
        self._resolved = False

    # ------------------------------------------------------------------
    # Lazy provider resolution
    # ------------------------------------------------------------------

    def _resolve_provider(self) -> tuple[str | None, str | None]:
        """Return (provider, api_key) or (None, None) if no key available."""
        if self._resolved:
            return self._provider, self._api_key

        self._resolved = True
        cfg = self._config

        # Explicit provider + key from config
        if cfg.api_key:
            self._provider = cfg.provider.lower()
            self._api_key = cfg.api_key
            return self._provider, self._api_key

        # Auto-detect from environment
        if os.environ.get("ANTHROPIC_API_KEY"):
            self._provider = "anthropic"
            self._api_key = os.environ["ANTHROPIC_API_KEY"]
        elif os.environ.get("OPENAI_API_KEY"):
            self._provider = "openai"
            self._api_key = os.environ["OPENAI_API_KEY"]
        else:
            logger.debug(
                "llm-extractor: no API key found (ANTHROPIC_API_KEY / OPENAI_API_KEY) — "
                "LLM relationship extraction disabled"
            )

        return self._provider, self._api_key

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, content: str) -> str:
        """Call the LLM and return the raw text response."""
        provider, api_key = self._resolve_provider()
        if provider is None or api_key is None:
            return ""

        prompt = _USER_TMPL.format(content=content[:4000])

        if provider == "anthropic":
            import anthropic  # type: ignore
            client = anthropic.AsyncAnthropic(api_key=api_key)
            response = await client.messages.create(
                model=self._config.model or "claude-haiku-4-5-20251001",
                max_tokens=self._config.max_tokens,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

        if provider == "openai":
            from openai import AsyncOpenAI  # type: ignore
            kwargs: dict = {"api_key": api_key}
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            client = AsyncOpenAI(**kwargs)
            response = await client.chat.completions.create(
                model=self._config.model or "gpt-4o-mini",
                max_tokens=self._config.max_tokens,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            return (response.choices[0].message.content or "").strip()

        logger.debug("llm-extractor: unknown provider %r", provider)
        return ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract(self, content: str) -> list[ExtractedRelationship]:
        """Extract typed relationships from *content*.

        Returns an empty list on any error — never raises.
        """
        provider, _ = self._resolve_provider()
        if provider is None:
            return []

        try:
            raw = await self._call_llm(content)
        except Exception as exc:
            logger.debug("llm-extractor: LLM call failed (non-fatal): %s", exc)
            return []

        if not raw:
            return []

        try:
            # Strip markdown fences if present
            clean = raw
            if clean.startswith("```"):
                clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
            data = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("llm-extractor: JSON parse failed (non-fatal): %s | raw=%r", exc, raw[:200])
            return []

        relationships: list[ExtractedRelationship] = []
        threshold = self._config.confidence_threshold

        for item in data.get("relationships", []):
            try:
                source = str(item.get("source", "")).strip().lower()[:100]
                target = str(item.get("target", "")).strip().lower()[:100]
                edge_type = str(item.get("edge_type", "")).upper().strip()
                confidence = float(item.get("confidence", 0.0))

                if not source or not target:
                    continue
                if confidence < threshold:
                    continue
                if edge_type not in EDGE_VOCABULARY:
                    logger.debug(
                        "llm-extractor: unknown edge type %r → falling back to RELATES_TO", edge_type
                    )
                    edge_type = _CATCHALL_EDGE

                relationships.append(
                    ExtractedRelationship(
                        source=source,
                        edge_type=edge_type,
                        target=target,
                        confidence=confidence,
                    )
                )
            except Exception as exc:
                logger.debug("llm-extractor: skipping malformed item %r: %s", item, exc)

        logger.debug("llm-extractor: extracted %d relationships", len(relationships))
        return relationships


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_extractor: LLMExtractor | None = None


def get_llm_extractor(config: "LLMExtractionConfig") -> LLMExtractor:
    """Return or create the module-level LLMExtractor singleton."""
    global _extractor
    if _extractor is None:
        _extractor = LLMExtractor(config)
    return _extractor


def reset_llm_extractor() -> None:
    """Reset singleton — used in tests."""
    global _extractor
    _extractor = None
