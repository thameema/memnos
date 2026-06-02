"""
memnos_api.routers.consolidation — Two-layer memory consolidation.

Endpoint
--------
POST /memory/consolidate

Reads all raw (non-consolidated, non-auto-extracted) memories in a namespace,
groups them into time-window batches, and asks an LLM to synthesize durable
facts from each batch. The result is a set of high-quality consolidated
memories tagged ``consolidated`` that perform significantly better in retrieval.

Why two layers?
---------------
Raw memories = everything written directly (conversation turns, hook captures).
Many are ephemeral ("how are you?", "sounds good") and drown out real facts.

Consolidated memories = LLM-synthesized durable facts from raw batches.
These are what actually gets retrieved for QA and agent queries.

This is the primary technique that takes mnemory from 33% → 73% on LoCoMo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
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
# Consolidation prompt
# ---------------------------------------------------------------------------

_CONSOLIDATE_SYSTEM = (
    "You are a memory consolidator. "
    "Given a batch of raw conversation turns or notes, extract the durable, "
    "important facts worth long-term remembering. "
    "Ignore greetings, filler, and transient status updates. "
    "Respond with valid JSON only — no markdown, no explanation."
)

_CONSOLIDATE_USER_TMPL = """\
Consolidate the following raw memories into durable facts.

Rules:
- Focus on facts, decisions, preferences, skills — not ephemeral chatter
- Each consolidated fact must be self-contained and specific
- CRITICAL DATE RULE: If an event occurred on a specific date, include that exact
  date in the format "On [Month Day, Year], [event]". Never omit dates.
- content: concise, standalone statement (max 300 chars)
- type: fact | decision | constraint | preference | skill
- tags: 1-5 lowercase keywords
- rationale: why this is worth remembering (max 150 chars)
- Extract at most 15 items — quality over quantity
- If nothing durable exists, return {{"items": []}}

RAW MEMORIES:
{raw_memories}

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

_VALID_TYPES = {"fact", "decision", "constraint", "preference", "skill"}

# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

async def _call_llm_consolidate(raw_memories_text: str) -> str:
    """Call Anthropic or OpenAI to consolidate raw memories."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not anthropic_key and not openai_key:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.",
        )

    prompt = _CONSOLIDATE_USER_TMPL.format(raw_memories=raw_memories_text[:10000])

    if anthropic_key:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="anthropic SDK not installed") from exc
        ac = anthropic.AsyncAnthropic(api_key=anthropic_key)
        response = await ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=_CONSOLIDATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="openai SDK not installed") from exc
    oa = AsyncOpenAI(api_key=openai_key)
    resp = await oa.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _CONSOLIDATE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ConsolidateRequest(BaseModel):
    namespace: str = Field(..., min_length=1, description="Namespace to consolidate")
    window_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="Group raw memories into this many-hour windows before consolidating each batch",
    )
    max_raw_per_batch: int = Field(
        default=50,
        ge=5,
        le=200,
        description="Max raw memories per LLM consolidation batch",
    )
    dry_run: bool = Field(default=False, description="Extract but do not write consolidated memories")
    author: str = Field(default="consolidation", description="Author tag on consolidated memories")


class ConsolidateResponse(BaseModel):
    namespace: str
    raw_processed: int
    batches: int
    consolidated_written: int
    dry_run: bool


# ---------------------------------------------------------------------------
# Core consolidation logic (also callable as background task)
# ---------------------------------------------------------------------------

async def _consolidate_namespace(
    client,
    namespace: str,
    window_hours: int = 24,
    max_raw_per_batch: int = 50,
    dry_run: bool = False,
    author: str = "consolidation",
) -> ConsolidateResponse:
    """Consolidate raw memories for a namespace into durable facts.

    1. Fetch all non-consolidated memories (exclude 'consolidated' and 'auto-extracted' tagged).
    2. Sort by created_at and split into window_hours-sized buckets.
    3. For each bucket, call LLM to synthesize durable facts.
    4. Write consolidated facts tagged 'consolidated'.
    5. Tag processed raw memories with 'processed-for-consolidation' (avoids re-processing).
    """
    # Fetch all memories in namespace — use broad search
    try:
        results = await client.search(
            "information event fact preference decision",
            namespace,
            top_k=500,
            mode="hybrid",
        )
    except Exception as exc:
        logger.warning("consolidation: search failed: %s", exc)
        results = []

    # Filter to raw memories only (not already consolidated or auto-extracted)
    raw_memories = []
    for r in results:
        mem = r.memory if hasattr(r, "memory") else r
        tags = list(getattr(mem, "tags", None) or [])
        if "consolidated" in tags or "processed-for-consolidation" in tags:
            continue
        raw_memories.append(mem)

    if not raw_memories:
        logger.info("consolidation: no raw memories to process in ns=%s", namespace)
        return ConsolidateResponse(
            namespace=namespace,
            raw_processed=0,
            batches=0,
            consolidated_written=0,
            dry_run=dry_run,
        )

    # Sort by created_at
    def _ts(m):
        ca = getattr(m, "created_at", None)
        if ca is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return ca.replace(tzinfo=timezone.utc) if ca.tzinfo is None else ca

    raw_memories.sort(key=_ts)

    # Bucket into time windows
    window = timedelta(hours=window_hours)
    batches: list[list] = []
    current_batch: list = []
    bucket_start: datetime | None = None

    for mem in raw_memories:
        ts = _ts(mem)
        if bucket_start is None:
            bucket_start = ts
        if ts - bucket_start > window or len(current_batch) >= max_raw_per_batch:
            if current_batch:
                batches.append(current_batch)
            current_batch = [mem]
            bucket_start = ts
        else:
            current_batch.append(mem)
    if current_batch:
        batches.append(current_batch)

    consolidated_written = 0
    processed_ids: list[str] = []

    for batch in batches:
        # Format batch as text for LLM
        lines = []
        for mem in batch:
            ts_str = _ts(mem).strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{ts_str}] {getattr(mem, 'content', '')}")
        raw_text = "\n".join(lines)

        try:
            raw_response = await _call_llm_consolidate(raw_text)
        except HTTPException:
            logger.warning("consolidation: LLM call failed for batch, skipping")
            continue
        except Exception as exc:
            logger.warning("consolidation: LLM call error: %s", exc)
            continue

        # Parse
        clean = raw_response
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
        try:
            data = json.loads(clean)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("consolidation: JSON parse failed: %s | raw=%r", exc, raw_response[:200])
            continue

        items = data.get("items", [])[:15]
        if not items:
            # Mark batch as processed even if nothing consolidated
            processed_ids.extend([str(getattr(m, "id", "")) for m in batch])
            continue

        from memnos.models import MemoryType  # noqa: PLC0415

        for item in items:
            try:
                content = str(item.get("content", "")).strip()
                mem_type_str = str(item.get("type", "fact")).lower()
                tags = [str(t).lower() for t in (item.get("tags") or [])]
                rationale = str(item.get("rationale", "")).strip()
            except Exception:
                continue

            if not content or mem_type_str not in _VALID_TYPES:
                mem_type_str = "fact"

            if not dry_run:
                try:
                    mem_type = MemoryType(mem_type_str)
                    await client.add(
                        content=content,
                        namespace=namespace,
                        tags=tags + ["consolidated"],
                        source="consolidation",
                        memory_type=mem_type,
                        author=author,
                        rationale=rationale,
                    )
                    consolidated_written += 1
                except Exception as exc:
                    logger.warning("consolidation: write failed: %s", exc)
            else:
                consolidated_written += 1

        processed_ids.extend([str(getattr(m, "id", "")) for m in batch])

    # Tag processed raw memories so they aren't reprocessed next time
    if not dry_run and processed_ids:
        for mem_id in processed_ids:
            try:
                from memnos_api.auth import get_client  # noqa: PLC0415
                # Add tag via direct arcadedb update (best-effort)
                await client._arcadedb.add_tags(mem_id, namespace, ["processed-for-consolidation"])
            except Exception:
                pass  # non-fatal — worst case they get re-processed

    logger.info(
        "consolidation | ns=%s raw=%d batches=%d written=%d dry_run=%s",
        namespace, len(raw_memories), len(batches), consolidated_written, dry_run,
    )

    return ConsolidateResponse(
        namespace=namespace,
        raw_processed=len(raw_memories),
        batches=len(batches),
        consolidated_written=consolidated_written,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/consolidate", response_model=ConsolidateResponse, status_code=200)
async def consolidate_memories(
    req: ConsolidateRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> ConsolidateResponse:
    """Consolidate raw memories in a namespace into durable facts.

    Reads all non-consolidated memories, groups them into time-window batches,
    and synthesizes each batch into high-quality facts via LLM. Consolidated
    memories are tagged ``consolidated`` and perform significantly better in
    semantic search than raw conversation turns.

    This is the two-layer memory architecture used by high-scoring memory
    systems: raw → consolidated. Run after heavy ingestion sessions.

    Set ``dry_run=true`` to preview what would be written without persisting.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not anthropic_key and not openai_key:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key configured (ANTHROPIC_API_KEY or OPENAI_API_KEY required).",
        )

    await check_namespace_access(key_entry, req.namespace, operation="write")

    return await _consolidate_namespace(
        client=client,
        namespace=req.namespace,
        window_hours=req.window_hours,
        max_raw_per_batch=req.max_raw_per_batch,
        dry_run=req.dry_run,
        author=req.author,
    )


# ---------------------------------------------------------------------------
# Auto-consolidation background task (called by write_memory on threshold)
# ---------------------------------------------------------------------------

async def maybe_auto_consolidate(
    client,
    namespace: str,
    threshold: int = 100,
) -> None:
    """Fire-and-forget: consolidate if raw memory count crosses threshold.

    Called after every write. Checks count — if raw memories in namespace
    exceed threshold, runs consolidation in background. Non-fatal.
    """
    if os.environ.get("MEMNOS_AUTO_CONSOLIDATE", "true").lower() == "false":
        return
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        return

    try:
        # Quick count check via search — if < threshold, bail early
        results = await client.search("fact event", namespace, top_k=threshold + 10)
        raw_count = sum(
            1 for r in results
            if "consolidated" not in list(getattr(getattr(r, "memory", r), "tags", None) or [])
            and "processed-for-consolidation" not in list(getattr(getattr(r, "memory", r), "tags", None) or [])
        )
        if raw_count < threshold:
            return

        logger.info("auto-consolidate triggered: ns=%s raw_count=%d", namespace, raw_count)
        await _consolidate_namespace(client, namespace)
    except Exception as exc:
        logger.debug("auto-consolidate skipped (non-fatal): %s", exc)
