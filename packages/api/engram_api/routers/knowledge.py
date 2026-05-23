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

from fastapi import APIRouter, Depends, HTTPException, Query

from engram_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key_entry,
)
from engram_api.schemas import (
    KnowledgeAskRequest,
    KnowledgeAnswerResponse,
    MemoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


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
