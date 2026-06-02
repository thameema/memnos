"""
memnos_api.routers.extract — Auto-extraction endpoint.

Endpoint
--------
POST /memory/extract

Takes raw conversation text, calls an LLM (Anthropic or OpenAI — auto-detected
from env vars ANTHROPIC_API_KEY / OPENAI_API_KEY), extracts facts/decisions/
constraints/preferences/skills, optionally deduplicates against existing
memories, and writes the survivors.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from memnos_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = (
    "You are a precise memory extractor. "
    "Given a piece of conversation or text, extract distinct facts, decisions, "
    "constraints, preferences, and skills worth remembering. "
    "Respond with valid JSON only — no markdown, no explanation."
)

_EXTRACT_USER_TMPL = """\
Extract memorable items from the following text.

ALLOWED TYPES (use exactly these strings):
  fact        — a factual statement or observation
  decision    — an architectural or product decision made
  constraint  — a rule or restriction that must always be respected
  preference  — a stated or implied preference or style choice
  skill       — a technique, workflow, or learned capability

Rules:
- Each item must be self-contained (understandable without the full conversation)
- content: concise, standalone statement (max 300 chars)
- type: one of the allowed types above
- tags: 1-5 lowercase keywords relevant to the item
- rationale: brief explanation of why this is worth remembering (max 150 chars)
- Extract at most 20 items
- Omit trivial, ephemeral, or duplicate items
- If nothing is worth remembering, return {{"items": []}}

TEXT:
{text}

Respond with JSON only:
{{
  "items": [
    {{
      "content": "...",
      "type": "fact",
      "tags": ["tag1", "tag2"],
      "rationale": "..."
    }},
    ...
  ]
}}
"""

# ---------------------------------------------------------------------------
# LLM call (mirrors llm_extractor._call_llm pattern)
# ---------------------------------------------------------------------------

async def _call_llm(text: str) -> str:
    """Call Anthropic or OpenAI to extract memories. Returns raw JSON string."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not anthropic_key and not openai_key:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.",
        )

    prompt = _EXTRACT_USER_TMPL.format(text=text[:8000])

    if anthropic_key:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="anthropic SDK not installed") from exc

        client = anthropic.AsyncAnthropic(api_key=anthropic_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    # OpenAI fallback
    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="openai SDK not installed") from exc

    oa_client = AsyncOpenAI(api_key=openai_key)
    response = await oa_client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Raw conversation or text to extract memories from")
    namespace: str = Field(..., min_length=1, description="Target namespace to write memories into")
    source: str = Field(default="extract", description="Source identifier for provenance")
    author: str = Field(default="", description="Author identifier")
    deduplicate: bool = Field(default=True, description="Skip items too similar to existing memories")
    dry_run: bool = Field(default=False, description="Extract but do not write to the store")
    max_similarity: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Similarity threshold above which an item is considered a duplicate",
    )


class ExtractedItem(BaseModel):
    content: str
    memory_type: str
    tags: list[str]
    rationale: str
    written: bool
    skip_reason: str = ""


class ExtractResponse(BaseModel):
    extracted: list[ExtractedItem]
    written: int
    skipped: int
    namespace: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

_VALID_TYPES: set[str] = {"fact", "decision", "constraint", "preference", "skill"}


@router.post("/extract", response_model=ExtractResponse, status_code=200)
async def extract_memories(
    req: ExtractRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> ExtractResponse:
    """Extract memorable items from raw text and optionally write them to the store.

    The endpoint:
    1. Calls an LLM (Anthropic preferred, OpenAI fallback) to identify up to 20
       facts/decisions/constraints/preferences/skills in the supplied text.
    2. For each item, if ``deduplicate=True``, searches for near-duplicate
       memories and skips items whose top-1 similarity exceeds ``max_similarity``.
    3. Unless ``dry_run=True``, writes non-duplicate items to the store.
    4. Returns a summary with every extracted item annotated with whether it was
       written or skipped (and why).
    """
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")

    await check_namespace_access(key_entry, req.namespace, operation="write")

    # --- Step 1: LLM extraction ---
    try:
        raw = await _call_llm(req.text)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("LLM extraction call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    # Parse LLM response (strip markdown fences if present)
    clean = raw
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)

    try:
        data = json.loads(clean)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("extract: JSON parse failed: %s | raw=%r", exc, raw[:300])
        raise HTTPException(status_code=502, detail=f"LLM returned unparseable JSON: {exc}") from exc

    raw_items = data.get("items", [])[:20]  # cap at 20

    # --- Steps 2 & 3: deduplicate and write ---
    results: list[ExtractedItem] = []
    written_count = 0
    skipped_count = 0

    from memnos.models import MemoryType  # noqa: PLC0415

    for raw_item in raw_items:
        try:
            content = str(raw_item.get("content", "")).strip()
            mem_type_str = str(raw_item.get("type", "fact")).lower().strip()
            tags = [str(t).lower() for t in (raw_item.get("tags") or [])]
            rationale = str(raw_item.get("rationale", "")).strip()
        except Exception as exc:
            logger.debug("extract: skipping malformed item %r: %s", raw_item, exc)
            continue

        if not content:
            continue

        if mem_type_str not in _VALID_TYPES:
            mem_type_str = "fact"

        # --- Deduplication ---
        skip_reason = ""
        if req.deduplicate:
            try:
                search_results = await client.search(content, req.namespace, top_k=1)
                if search_results:
                    top_score = float(getattr(search_results[0], "score", 0.0))
                    if top_score >= req.max_similarity:
                        skip_reason = f"duplicate (similarity={top_score:.3f})"
            except Exception as exc:
                logger.debug("extract: dedup search failed (non-fatal): %s", exc)

        was_written = False
        if not skip_reason:
            if not req.dry_run:
                try:
                    try:
                        mem_type = MemoryType(mem_type_str)
                    except ValueError:
                        mem_type = MemoryType.fact

                    await client.add(
                        content=content,
                        namespace=req.namespace,
                        tags=tags,
                        source=req.source,
                        memory_type=mem_type,
                        author=req.author,
                        rationale=rationale,
                    )
                    was_written = True
                    written_count += 1
                except Exception as exc:
                    logger.warning("extract: write failed for item %r: %s", content[:80], exc)
                    skip_reason = f"write error: {exc}"
                    skipped_count += 1
            else:
                # dry_run — counted as "would write" but not persisted
                was_written = True
                written_count += 1
        else:
            skipped_count += 1

        results.append(
            ExtractedItem(
                content=content,
                memory_type=mem_type_str,
                tags=tags,
                rationale=rationale,
                written=was_written,
                skip_reason=skip_reason,
            )
        )

    logger.info(
        "extract | ns=%s user=%s items=%d written=%d skipped=%d dry_run=%s",
        req.namespace, user_id, len(results), written_count, skipped_count, req.dry_run,
    )

    return ExtractResponse(
        extracted=results,
        written=written_count,
        skipped=skipped_count,
        namespace=req.namespace,
    )
