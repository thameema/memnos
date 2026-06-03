"""
memnos_api.routers.context — Context injection endpoint.

Endpoint
--------
POST /memory/context

Retrieves the most relevant memories for a query and formats them as a
ready-to-inject context block for any LLM prompt. Unlike a raw search,
this endpoint:

  1. Deduplicates semantically similar results (keeps highest-scored)
  2. Groups by memory type (facts / decisions / preferences / skills)
  3. Surfaces contradiction warnings inline so the LLM knows when beliefs conflict
  4. Includes source attribution (author, created_at, namespace)
  5. Returns token estimate so callers can budget context size
  6. Supports multiple output formats: xml | markdown | json | plain

Why this matters (context engineering, 2025):
  Mem0 and Zep inject raw memory dumps into prompts. memnos injects a
  *curated, attributed, contradiction-aware* block — letting the LLM
  reason about recency and conflicts rather than treating all memories
  as equally authoritative.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from memnos_api.auth import (
    check_namespace_access,
    get_accessible_namespaces,
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])

# Rough chars-per-token estimate (GPT-4 / Claude tokenisers are ~4 chars/token)
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ContextRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The question or task the agent is about to perform")
    namespace: str = Field(
        default="all",
        description="Namespace to retrieve from. 'all' searches all accessible namespaces.",
    )
    top_k: int = Field(default=10, ge=1, le=50, description="Max memories to retrieve")
    format: Literal["xml", "markdown", "json", "plain"] = Field(
        default="xml",
        description=(
            "Output format. xml = <memory>...</memory> block (recommended for Claude/GPT), "
            "markdown = fenced sections, json = structured, plain = bare text"
        ),
    )
    include_attribution: bool = Field(
        default=True,
        description="Include source author, date, and namespace in the context block",
    )
    include_contradictions: bool = Field(
        default=True,
        description="Highlight memories that contradict each other so the LLM can reason about recency",
    )
    min_score: float = Field(
        default=0.35, ge=0.0, le=1.0,
        description="Minimum similarity score; lower = more recall, higher = more precision",
    )
    deduplicate: bool = Field(
        default=True,
        description="Drop semantically near-duplicate memories (keeps highest-scored)",
    )
    dedup_threshold: float = Field(
        default=0.92, ge=0.0, le=1.0,
        description="Cosine similarity above which two memories are considered duplicates",
    )


class ContextMemoryItem(BaseModel):
    id: str
    content: str
    memory_type: str
    score: float
    namespace: str
    author: str = ""
    created_at: str = ""
    tags: list[str] = []
    contradicts: list[str] = []  # IDs of memories this one contradicts


class ContextResponse(BaseModel):
    context_block: str = Field(description="Ready-to-inject formatted context string")
    memories: list[ContextMemoryItem] = Field(description="Structured memory list for programmatic use")
    memory_count: int
    token_estimate: int = Field(description="Rough token count of context_block (~4 chars/token)")
    has_contradictions: bool
    query: str
    namespace: str
    format: str


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_item(mem: ContextMemoryItem, include_attribution: bool) -> str:
    lines = [mem.content]
    if include_attribution:
        parts = []
        if mem.author:
            parts.append(f"author={mem.author}")
        if mem.created_at:
            parts.append(f"date={mem.created_at[:10]}")
        if mem.namespace and mem.namespace != "all":
            parts.append(f"ns={mem.namespace}")
        if parts:
            lines.append(f"  [{', '.join(parts)}]")
    if mem.contradicts:
        lines.append(f"  ⚠ contradicts: {', '.join(mem.contradicts[:2])}")
    return "\n".join(lines)


def _build_xml(
    memories: list[ContextMemoryItem],
    query: str,
    include_attribution: bool,
    has_contradictions: bool,
) -> str:
    groups: dict[str, list[ContextMemoryItem]] = {}
    for m in memories:
        groups.setdefault(m.memory_type, []).append(m)

    lines = ['<memory>']
    lines.append(f'  <query>{query}</query>')

    if has_contradictions:
        lines.append('  <note>Some memories conflict — prefer more recent entries (higher date).</note>')

    for mtype, items in groups.items():
        lines.append(f'  <{mtype}s>')
        for item in items:
            lines.append(f'    <item score="{item.score:.2f}">')
            lines.append(f'      {item.content}')
            if include_attribution and (item.author or item.created_at):
                attr_parts = []
                if item.author:
                    attr_parts.append(f'author="{item.author}"')
                if item.created_at:
                    attr_parts.append(f'date="{item.created_at[:10]}"')
                lines.append(f'      <!-- {" ".join(attr_parts)} -->')
            if item.contradicts:
                lines.append(f'      <!-- ⚠ contradicts: {", ".join(item.contradicts[:2])} -->')
            lines.append(f'    </item>')
        lines.append(f'  </{mtype}s>')

    lines.append('</memory>')
    return "\n".join(lines)


def _build_markdown(
    memories: list[ContextMemoryItem],
    query: str,
    include_attribution: bool,
    has_contradictions: bool,
) -> str:
    groups: dict[str, list[ContextMemoryItem]] = {}
    for m in memories:
        groups.setdefault(m.memory_type, []).append(m)

    lines = ['## Relevant Memory Context', f'*Query: {query}*', '']

    if has_contradictions:
        lines += ['> **Note:** Some memories conflict — prefer more recent entries.', '']

    for mtype, items in groups.items():
        lines.append(f'### {mtype.title()}s')
        for item in items:
            bullet = f'- {item.content}'
            if include_attribution and (item.author or item.created_at):
                attr = " | ".join(filter(None, [
                    item.author,
                    item.created_at[:10] if item.created_at else "",
                ]))
                bullet += f' *(source: {attr})*'
            if item.contradicts:
                bullet += f' ⚠'
            lines.append(bullet)
        lines.append('')

    return "\n".join(lines).rstrip()


def _build_plain(memories: list[ContextMemoryItem], query: str, include_attribution: bool) -> str:
    lines = [f'Context for: {query}', '---']
    for m in memories:
        line = m.content
        if include_attribution and m.created_at:
            line += f' ({m.created_at[:10]})'
        lines.append(line)
    return "\n".join(lines)


def _build_json_block(memories: list[ContextMemoryItem], query: str) -> str:
    import json
    return json.dumps({
        "query": query,
        "memories": [m.model_dump() for m in memories],
    }, indent=2)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(items: list[ContextMemoryItem], threshold: float) -> list[ContextMemoryItem]:
    """Keep highest-scored memory when two are near-identical (same score cluster)."""
    kept: list[ContextMemoryItem] = []
    seen_content: list[str] = []

    for item in sorted(items, key=lambda x: x.score, reverse=True):
        content_lower = item.content.lower().strip()
        # Simple trigram overlap check — avoids LLM call for dedup
        is_dup = False
        for seen in seen_content:
            overlap = _trigram_overlap(content_lower, seen)
            if overlap >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(item)
            seen_content.append(content_lower)

    return kept


def _trigram_overlap(a: str, b: str) -> float:
    """Jaccard similarity of character trigrams — fast dedup proxy."""
    def trigrams(s: str) -> set:
        return {s[i:i+3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}
    ta, tb = trigrams(a), trigrams(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/context", response_model=ContextResponse, status_code=200)
async def get_context(
    req: ContextRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> ContextResponse:
    """Return a formatted, ready-to-inject context block for a given query.

    This is the primary endpoint for *context engineering* — injecting
    memnos memory into an LLM prompt before generation.

    Unlike raw search, this endpoint:
    - Deduplicates near-identical memories
    - Groups by memory type (facts, decisions, preferences, skills)
    - Surfaces contradictions so the LLM can reason about recency
    - Includes source attribution for trust and auditability
    - Returns token estimate for prompt budget management
    - Formats the block for direct injection (xml recommended for Claude/GPT)

    Example usage in an agent::

        ctx = POST /memory/context {"query": "What does the user prefer?", "namespace": "user:alice"}
        prompt = f"{system_prompt}\\n\\n{ctx.context_block}\\n\\nUser: {user_message}"
    """
    _ns_raw = (req.namespace or "").strip()
    is_all = _ns_raw in ("", "all", "*")

    if not is_all:
        await check_namespace_access(key_entry, _ns_raw)

    # -- Search --
    try:
        if is_all:
            from memnos_api.auth import get_accessible_namespaces  # noqa: PLC0415
            try:
                db_namespaces = await client._arcadedb.list_namespaces()
            except Exception:
                db_namespaces = []
            accessible = set(get_accessible_namespaces(key_entry, db_namespaces))
            raw_results = await client.search(req.query, "all", req.top_k * 2, mode="hybrid")
            raw_results = [r for r in raw_results if r.memory.namespace in accessible]
        else:
            raw_results = await client.search(req.query, _ns_raw, req.top_k * 2, mode="hybrid")
    except Exception as exc:
        logger.exception("context: search failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if raw_results is None:
        raw_results = []

    # -- Score floor --
    raw_results = [r for r in raw_results if r.score >= req.min_score]
    raw_results.sort(key=lambda r: r.score, reverse=True)

    # -- Build ContextMemoryItem list --
    items: list[ContextMemoryItem] = []
    for r in raw_results[:req.top_k * 2]:
        mem = r.memory
        created_str = ""
        ca = getattr(mem, "created_at", None)
        if ca:
            if hasattr(ca, "isoformat"):
                created_str = ca.isoformat()
            else:
                created_str = str(ca)

        items.append(ContextMemoryItem(
            id=str(mem.id),
            content=mem.content,
            memory_type=mem.memory_type.value if hasattr(mem.memory_type, "value") else str(mem.memory_type),
            score=float(r.score),
            namespace=mem.namespace,
            author=getattr(mem, "author", "") or "",
            created_at=created_str,
            tags=list(getattr(mem, "tags", None) or []),
            contradicts=[],
        ))

    # -- Deduplication --
    if req.deduplicate:
        items = _deduplicate(items, req.dedup_threshold)

    items = items[:req.top_k]

    # -- Contradiction detection (lightweight — check superseded flag & affects) --
    has_contradictions = False
    if req.include_contradictions:
        item_ids = {it.id for it in items}
        for item in items:
            mem_obj = next(
                (r.memory for r in raw_results if str(r.memory.id) == item.id), None
            )
            if mem_obj is None:
                continue
            affects = list(getattr(mem_obj, "affects", None) or [])
            for affected_id in affects:
                if affected_id in item_ids:
                    item.contradicts.append(affected_id)
                    has_contradictions = True

    # -- Format --
    fmt = req.format
    if fmt == "xml":
        block = _build_xml(items, req.query, req.include_attribution, has_contradictions)
    elif fmt == "markdown":
        block = _build_markdown(items, req.query, req.include_attribution, has_contradictions)
    elif fmt == "json":
        block = _build_json_block(items, req.query)
    else:  # plain
        block = _build_plain(items, req.query, req.include_attribution)

    token_estimate = max(1, len(block) // _CHARS_PER_TOKEN)

    logger.info(
        "context | ns=%s user=%s memories=%d tokens≈%d format=%s contradictions=%s",
        req.namespace, user_id, len(items), token_estimate, fmt, has_contradictions,
    )

    return ContextResponse(
        context_block=block,
        memories=items,
        memory_count=len(items),
        token_estimate=token_estimate,
        has_contradictions=has_contradictions,
        query=req.query,
        namespace=req.namespace,
        format=fmt,
    )
