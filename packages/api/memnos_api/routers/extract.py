"""
memnos_api.routers.extract — Auto-extraction endpoint.

Endpoint
--------
POST /memory/extract

Takes raw conversation text, calls an LLM (Anthropic or OpenAI — auto-detected
from env vars ANTHROPIC_API_KEY / OPENAI_API_KEY), extracts facts/decisions/
constraints/preferences/skills, deduplicates against existing memories in a
single unified LLM call, and writes the survivors.

Unified atomic extraction (v2)
-------------------------------
Old approach: 1 extract call + N per-item dedup searches (N+1 total).
New approach:
  1. Bulk search — top-5 similar existing memories for the full text (1 call).
  2. Single LLM call that sees both the text AND existing memories; returns
     ADD / UPDATE / SKIP per item atomically.
  3. Write ADD items; write + supersede for UPDATE items.

The ``deduplicate`` and ``max_similarity`` fields on ExtractRequest are kept
for API compatibility but are no-ops — the LLM now owns dedup decisions.
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
Extract memorable items from the following text, then decide what to do with each.

ALLOWED TYPES (use exactly these strings):
  fact        — a factual statement or observation
  decision    — an architectural or product decision made
  constraint  — a rule or restriction that must always be respected
  preference  — a stated or implied preference or style choice
  skill       — a technique, workflow, or learned capability

ACTIONS:
  ADD     — new information not yet in memory
  UPDATE  — replaces an existing memory (provide its id in updates_id)
  SKIP    — already captured in an existing memory

EXISTING MEMORIES (already stored — use their IDs in updates_id when updating):
{existing}

CRITICAL DATE RULE: If an event occurred on a specific date, the content MUST
include that exact date in the format "On [Month Day, Year], [event]".
Example: "On March 15, 2023, Caroline attended the LGBTQ support group."
Never omit dates when they appear in the source text.

Rules:
- Each item must be self-contained (understandable without the full conversation)
- content: concise, standalone statement (max 300 chars)
- type: one of the allowed types above
- tags: 1-5 lowercase keywords relevant to the item
- rationale: brief explanation of why this is worth remembering (max 150 chars)
- action: ADD, UPDATE, or SKIP
- updates_id: the id of the existing memory being updated (only for UPDATE action)
- Extract at most 20 items
- Omit trivial or ephemeral items
- If nothing is worth extracting, return {{"items": []}}

TEXT:
{text}

Respond with JSON only:
{{
  "items": [
    {{
      "content": "...",
      "type": "fact",
      "tags": ["tag1", "tag2"],
      "rationale": "...",
      "action": "ADD",
      "updates_id": null
    }},
    ...
  ]
}}
"""


# ---------------------------------------------------------------------------
# LLM call — unified (text + existing memories context)
# ---------------------------------------------------------------------------

async def _call_llm_unified(text: str, existing_memories: list[dict]) -> str:
    """Call Anthropic or OpenAI with text and existing memory context.

    Returns a raw JSON string containing items with ADD/UPDATE/SKIP actions.

    Args:
        text: The source text to extract memories from.
        existing_memories: List of dicts with ``id`` and ``content`` keys
            representing already-stored memories that may be relevant.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not anthropic_key and not openai_key:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.",
        )

    if existing_memories:
        existing_block = "\n".join(
            f"{i + 1}. [id={m['id']}] {m['content']}"
            for i, m in enumerate(existing_memories)
        )
    else:
        existing_block = "(none yet)"

    prompt = _EXTRACT_USER_TMPL.format(
        existing=existing_block,
        text=text[:8000],
    )

    if anthropic_key:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="anthropic SDK not installed") from exc

        ac = anthropic.AsyncAnthropic(api_key=anthropic_key)
        response = await ac.messages.create(
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


def _parse_llm_response(raw: str) -> list[dict]:
    """Strip optional markdown fences and parse the JSON items list."""
    clean = raw
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
    data = json.loads(clean)
    return data.get("items", [])[:20]


# ---------------------------------------------------------------------------
# Background extraction helper (called by write_memory on every ingest)
# ---------------------------------------------------------------------------

async def _extract_and_write(
    client,
    text: str,
    namespace: str,
    author: str = "",
    source: str = "auto-extract",
    max_similarity: float = 0.92,  # kept for signature compat — no-op
) -> None:
    """Fire-and-forget: extract facts from *text* and write non-duplicate items.

    Called as an asyncio background task after every memory write when an LLM
    API key is configured.  Silently returns on any failure so it never affects
    the caller.  Items written here carry the ``auto-extracted`` tag so they
    are not re-extracted on their own ingest.

    Uses unified atomic extraction:
      1. Bulk search (top-5) for existing similar context — 1 call.
      2. Single LLM call that extracts AND decides ADD/UPDATE/SKIP.
      3. Write ADDs; write + supersede for UPDATEs.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not anthropic_key and not openai_key:
        return  # no LLM configured — skip silently

    if len(text.strip()) < 80:
        return  # too short to be worth an LLM call

    # Step 1: bulk search for existing similar context
    existing: list[dict] = []
    try:
        hits = await client.search(text[:500], namespace, top_k=5)
        existing = [
            {
                "id": r.memory.id if hasattr(r, "memory") else r.get("id", ""),
                "content": (
                    r.memory.content if hasattr(r, "memory") else r.get("content", "")
                )[:200],
            }
            for r in hits
        ]
    except Exception:
        pass  # proceed without context

    # Step 2: single unified LLM call
    try:
        raw = await _call_llm_unified(text, existing)
    except Exception as exc:
        logger.debug("auto-extract: LLM call failed (non-fatal): %s", exc)
        return

    try:
        raw_items = _parse_llm_response(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("auto-extract: JSON parse failed: %s | raw=%r", exc, raw[:200])
        return

    from memnos.models import MemoryType  # noqa: PLC0415

    # Step 3: act on each item
    written = 0
    for raw_item in raw_items:
        try:
            content = str(raw_item.get("content", "")).strip()
            mem_type_str = str(raw_item.get("type", "fact")).lower().strip()
            tags = [str(t).lower() for t in (raw_item.get("tags") or [])]
            rationale = str(raw_item.get("rationale", "")).strip()
            action = str(raw_item.get("action", "ADD")).upper().strip()
            updates_id = raw_item.get("updates_id") or None
        except Exception:
            continue

        if not content:
            continue
        if action == "SKIP":
            continue
        if mem_type_str not in _VALID_TYPES:
            mem_type_str = "fact"

        try:
            mem_type = MemoryType(mem_type_str) if mem_type_str in _VALID_TYPES else MemoryType.fact
            await client.add(
                content=content,
                namespace=namespace,
                tags=tags + ["auto-extracted"],
                source=source,
                memory_type=mem_type,
                author=author,
                rationale=rationale,
            )
            written += 1

            if action == "UPDATE" and updates_id:
                try:
                    await client.supersede(str(updates_id), namespace)
                except Exception as exc:
                    logger.debug("auto-extract: supersede failed (non-fatal): %s", exc)

        except Exception as exc:
            logger.debug("auto-extract: write failed for item %r: %s", content[:80], exc)

    if written:
        logger.info("auto-extract | ns=%s written=%d from %d chars", namespace, written, len(text))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Raw conversation or text to extract memories from")
    namespace: str = Field(..., min_length=1, description="Target namespace to write memories into")
    source: str = Field(default="extract", description="Source identifier for provenance")
    author: str = Field(default="", description="Author identifier")
    deduplicate: bool = Field(
        default=True,
        description="Kept for API compatibility — dedup is now handled by the LLM atomically",
    )
    dry_run: bool = Field(default=False, description="Extract but do not write to the store")
    max_similarity: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Kept for API compatibility — dedup threshold is now owned by the LLM",
    )


class ExtractedItem(BaseModel):
    content: str
    memory_type: str
    tags: list[str]
    rationale: str
    written: bool
    skip_reason: str = ""
    action: str = "ADD"  # ADD | UPDATE | SKIP


class ExtractResponse(BaseModel):
    extracted: list[ExtractedItem]
    written: int
    skipped: int
    namespace: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TYPES: set[str] = {"fact", "decision", "constraint", "preference", "skill"}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/extract", response_model=ExtractResponse, status_code=200)
async def extract_memories(
    req: ExtractRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> ExtractResponse:
    """Extract memorable items from raw text and optionally write them to the store.

    Unified atomic extraction (v2):
      1. Bulk search — find top-5 similar existing memories for the full text
         (1 search call total, not per-item).
      2. Single LLM call that sees both the text AND existing memories; returns
         ADD / UPDATE / SKIP per item atomically.
      3. ADD items are written to the store; UPDATE items are written and the
         superseded memory is marked stale via client.supersede().

    The ``deduplicate`` and ``max_similarity`` request fields are accepted for
    backward compatibility but are no-ops — dedup is now owned by the LLM.
    """
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")

    await check_namespace_access(key_entry, req.namespace, operation="write")

    # Step 1: bulk search for existing similar context (1 call)
    existing: list[dict] = []
    try:
        hits = await client.search(req.text[:500], req.namespace, top_k=5)
        existing = [
            {
                "id": r.memory.id if hasattr(r, "memory") else r.get("id", ""),
                "content": (
                    r.memory.content if hasattr(r, "memory") else r.get("content", "")
                )[:200],
            }
            for r in hits
        ]
    except Exception as exc:
        logger.debug("extract: bulk search failed (non-fatal, proceeding without context): %s", exc)

    # Step 2: single unified LLM call
    try:
        raw = await _call_llm_unified(req.text, existing)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("LLM extraction call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    # Parse LLM response
    try:
        raw_items = _parse_llm_response(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("extract: JSON parse failed: %s | raw=%r", exc, raw[:300])
        raise HTTPException(status_code=502, detail=f"LLM returned unparseable JSON: {exc}") from exc

    # Step 3: act on each item per action decision
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
            action = str(raw_item.get("action", "ADD")).upper().strip()
            updates_id = raw_item.get("updates_id") or None
        except Exception as exc:
            logger.debug("extract: skipping malformed item %r: %s", raw_item, exc)
            continue

        if not content:
            continue

        if mem_type_str not in _VALID_TYPES:
            mem_type_str = "fact"

        # LLM decided to skip — already in memory
        if action == "SKIP":
            skipped_count += 1
            results.append(
                ExtractedItem(
                    content=content,
                    memory_type=mem_type_str,
                    tags=tags,
                    rationale=rationale,
                    written=False,
                    skip_reason="llm_decided_skip",
                    action="SKIP",
                )
            )
            continue

        # ADD or UPDATE
        was_written = False
        skip_reason = ""

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

                if action == "UPDATE" and updates_id:
                    try:
                        await client.supersede(str(updates_id), req.namespace)
                    except Exception as exc:
                        logger.debug("extract: supersede failed (non-fatal): %s", exc)

            except Exception as exc:
                logger.warning("extract: write failed for item %r: %s", content[:80], exc)
                skip_reason = f"write error: {exc}"
                skipped_count += 1
        else:
            # dry_run — counted as "would write" but not persisted
            was_written = True
            written_count += 1

        results.append(
            ExtractedItem(
                content=content,
                memory_type=mem_type_str,
                tags=tags,
                rationale=rationale,
                written=was_written,
                skip_reason=skip_reason,
                action=action,
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
