"""
engram_api.routers.knowledge — High-level knowledge base Q&A endpoint.

POST /knowledge/ask
  Accepts a question, searches relevant memories in a namespace,
  assembles them as context, calls the Anthropic API, and returns
  a synthesised answer with sources.

GET /knowledge/search
  Thin wrapper around memory search optimised for Q&A usage patterns.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from engram_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)
from engram_api.schemas import (
    KnowledgeAskRequest,
    KnowledgeAnswerResponse,
    MemoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Knowledge health models
# ---------------------------------------------------------------------------

class HealthIssue(BaseModel):
    level: str         # "warning" | "info" | "critical"
    message: str
    affected_ids: list[str] = []


class KnowledgeHealthReport(BaseModel):
    namespace: str
    generated_at: datetime
    health_score: int              # 0-100 (100 = healthy)
    total_memories: int
    metrics: dict[str, Any]
    issues: list[HealthIssue]


def _compute_health_score(
    unused_constraints: int,
    stale_child_namespaces: int,
    overdue_reviews: int,
    approaching_expiry: int,
) -> int:
    score = 100
    score -= min(unused_constraints * 3, 15)
    score -= min(stale_child_namespaces * 5, 20)
    score -= min(overdue_reviews * 2, 20)
    score -= min(approaching_expiry * 1, 10)
    return max(0, score)


def _to_response(memory, score: float | None = None) -> MemoryResponse:
    """Convert a MemoryEntry model to a MemoryResponse."""
    return MemoryResponse(
        id=str(memory.id),
        content=memory.content,
        namespace=memory.namespace,
        created_at=memory.created_at,
        tags=list(memory.tags or []),
        score=score,
    )


# ---------------------------------------------------------------------------
# POST /knowledge/ask
# ---------------------------------------------------------------------------

@router.post("/ask", response_model=KnowledgeAnswerResponse)
async def knowledge_ask(
    req: KnowledgeAskRequest,
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> KnowledgeAnswerResponse:
    """
    Answer a natural-language question using memories stored in *namespace*.

    The endpoint:
    1. Searches the top-*k* most relevant memories (hybrid mode).
    2. Assembles them into a context block.
    3. Sends the question + context to the Anthropic API.
    4. Returns the synthesised answer along with the source memories.

    Read-only keys may access this endpoint (it is a read operation).
    """
    # Read-only keys can use this endpoint (operation="read" by default)
    await check_namespace_access(key_entry, req.namespace)

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key or anthropic_api_key.startswith("sk-ant-placeholder"):
        raise HTTPException(
            status_code=503,
            detail=(
                "Knowledge Q&A is not available: ANTHROPIC_API_KEY is not configured. "
                "Core memory operations (write, search, graph) work without it."
            ),
        )

    # 1. Search memories
    try:
        results = await client.search(req.question, req.namespace, req.top_k, "hybrid")
    except Exception as exc:
        logger.exception("Memory search failed during knowledge/ask: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    results = results or []
    source_memories = [_to_response(r.memory, score=r.score) for r in results]

    # 2. Build context block from top results
    if source_memories:
        context_lines = []
        for i, mem in enumerate(source_memories, start=1):
            context_lines.append(f"[{i}] {mem.content}")
        context_block = "\n\n".join(context_lines)
    else:
        context_block = "(No relevant memories found.)"

    user_message = (
        f"Question: {req.question}\n\n"
        f"Context from knowledge base:\n{context_block}"
    )

    # 3. Call Anthropic API
    try:
        import anthropic  # type: ignore

        aclient = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
        response = await aclient.messages.create(
            model=req.model,
            max_tokens=1024,
            system=(
                "You are a knowledge base assistant. "
                "Answer questions using only the provided context. "
                "If the context does not contain enough information to answer, say so."
            ),
            messages=[{"role": "user", "content": user_message}],
        )
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="anthropic package is not installed. Run: pip install anthropic",
        )
    except Exception as exc:
        exc_str = str(exc).lower()
        if any(word in exc_str for word in ("authentication", "invalid x-api-key", "unauthorized", "401")):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Knowledge Q&A is not available: the configured ANTHROPIC_API_KEY "
                    "is invalid. Core memory operations are unaffected."
                ),
            ) from exc
        logger.exception("Anthropic API call failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API error: {exc}",
        ) from exc

    # 4. Extract answer and token usage
    answer_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            answer_text += block.text

    tokens_used = 0
    usage = getattr(response, "usage", None)
    if usage is not None:
        tokens_used = getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)

    return KnowledgeAnswerResponse(
        answer=answer_text,
        sources=source_memories,
        namespace=req.namespace,
        model_used=req.model,
        tokens_used=tokens_used,
    )


# ---------------------------------------------------------------------------
# GET /knowledge/search
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[MemoryResponse])
async def knowledge_search(
    q: str = Query(..., description="Natural-language search query"),
    ns: str = Query(..., description="Namespace to search within"),
    top_k: int = Query(5, ge=1, le=100),
    mode: str = Query("hybrid", description="hybrid | vector | graph"),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[MemoryResponse]:
    """
    Search memories in *ns* and return ranked results.

    This is a thin wrapper around the memory search endpoint, placed under
    the ``/knowledge`` prefix for semantic clarity in Q&A usage patterns.
    """
    await check_namespace_access(key_entry, ns)
    logger.debug(
        "knowledge_search | ns=%s mode=%s top_k=%d q=%r",
        ns,
        mode,
        top_k,
        q[:120],
    )
    try:
        results = await client.search(q, ns, top_k, mode)
    except Exception as exc:
        logger.exception("Knowledge search failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if results is None:
        return []

    return [_to_response(r.memory, score=r.score) for r in results]


# ---------------------------------------------------------------------------
# GET /knowledge/health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=KnowledgeHealthReport)
async def knowledge_health(
    ns: str = Query(..., description="Namespace to audit"),
    stale_days: int = Query(30, ge=1, description="Days without writes before a namespace is considered stale"),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> KnowledgeHealthReport:
    """
    Return a knowledge health report for *ns*.

    Metrics
    -------
    - unused_constraints      : active constraints with no affects targets
    - stale_child_namespaces  : child namespaces with no writes in stale_days
    - overdue_reviews         : memories past their review_by date
    - approaching_expiry      : memories expiring within 7 days
    - health_score            : composite 0-100 (100 = healthy)
    """
    await check_namespace_access(key_entry, ns)
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=stale_days)

    issues: list[HealthIssue] = []
    metrics: dict = {}

    # Total memory count
    try:
        total = await client._arcadedb.count_memories(ns)
    except Exception:
        total = -1
    metrics["total_memories"] = total

    # Unused constraints
    unused_constraints: list = []
    try:
        constraints = await client._arcadedb.get_unused_constraints(ns)
        unused_constraints = constraints
        metrics["unused_constraints"] = [str(c.id) for c in constraints]
        if constraints:
            issues.append(HealthIssue(
                level="warning",
                message=f"{len(constraints)} constraint(s) have no AFFECTS targets — may not be enforced",
                affected_ids=[str(c.id) for c in constraints],
            ))
    except Exception as exc:
        logger.debug("unused_constraints check failed: %s", exc)
        metrics["unused_constraints"] = []

    # Stale child namespaces
    stale_namespaces: list[str] = []
    try:
        last_writes = await client._arcadedb.get_namespace_last_writes(ns)
        for child_ns, last_write_iso in last_writes.items():
            try:
                lw = datetime.fromisoformat(last_write_iso.replace("Z", "+00:00"))
                if lw.tzinfo is None:
                    lw = lw.replace(tzinfo=timezone.utc)
                if lw < stale_cutoff:
                    stale_namespaces.append(child_ns)
            except Exception:
                pass
        metrics["stale_child_namespaces"] = stale_namespaces
        if stale_namespaces:
            issues.append(HealthIssue(
                level="info",
                message=f"{len(stale_namespaces)} child namespace(s) have no writes in {stale_days} days",
                affected_ids=stale_namespaces,
            ))
    except Exception as exc:
        logger.debug("stale namespaces check failed: %s", exc)
        metrics["stale_child_namespaces"] = []

    # Overdue reviews
    overdue_count = 0
    try:
        overdue = await client._arcadedb.get_review_due(ns, limit=100)
        overdue_count = len(overdue)
        metrics["overdue_reviews"] = overdue_count
        if overdue_count > 0:
            issues.append(HealthIssue(
                level="warning",
                message=f"{overdue_count} memory/memories overdue for review",
                affected_ids=[str(m.id) for m in overdue],
            ))
    except Exception as exc:
        logger.debug("overdue reviews check failed: %s", exc)
        metrics["overdue_reviews"] = 0

    # Approaching expiry
    expiry_count = 0
    try:
        expiry_count = await client._arcadedb.count_approaching_expiry(ns, days=7)
        metrics["approaching_expiry_7d"] = expiry_count
        if expiry_count > 0:
            issues.append(HealthIssue(
                level="warning",
                message=f"{expiry_count} memory/memories expiring within 7 days",
            ))
    except Exception as exc:
        logger.debug("approaching expiry check failed: %s", exc)
        metrics["approaching_expiry_7d"] = 0

    health_score = _compute_health_score(
        unused_constraints=len(unused_constraints),
        stale_child_namespaces=len(stale_namespaces),
        overdue_reviews=overdue_count,
        approaching_expiry=expiry_count,
    )
    if health_score == 100:
        issues.append(HealthIssue(level="info", message="Namespace is healthy — no issues found"))

    return KnowledgeHealthReport(
        namespace=ns,
        generated_at=now,
        health_score=health_score,
        total_memories=total,
        metrics=metrics,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# GET /knowledge/communities  (Feature 3.4)
# ---------------------------------------------------------------------------

@router.get("/communities")
async def get_communities(
    ns: str = Query(...),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Return all detected communities for a namespace."""
    await check_namespace_access(key_entry, ns)
    communities = await client._arcadedb.list_communities(ns)
    return {"communities": communities, "count": len(communities), "namespace": ns}
