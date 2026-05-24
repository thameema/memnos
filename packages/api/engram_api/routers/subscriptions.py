"""
engram_api.routers.subscriptions — Namespace pub-sub endpoints.

Endpoints
---------
POST   /subscriptions/              — subscribe to a namespace
GET    /subscriptions/{ns}/feed     — poll for new memories since last seen
GET    /subscriptions/{ns}/stream   — SSE push stream (delivery_mode=immediate)
DELETE /subscriptions/{ns}          — unsubscribe
"""

from __future__ import annotations
from datetime import datetime
from typing import Any
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from engram_api.auth import check_namespace_access, get_client, require_api_key, require_api_key_entry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


class SubscribeRequest(BaseModel):
    namespace: str
    filter_types: list[str] = []          # [] = all memory types
    delivery_namespace: str = ""          # if set, new memories are pushed here (fan-out)
    delivery_mode: str = "cursor"         # "cursor" | "webhook" | "immediate"
    webhook_url: str = ""                 # HTTPS endpoint for webhook delivery


class SubscribeFeedItem(BaseModel):
    id: str
    content: str
    namespace: str
    memory_type: str
    author: str
    created_at: datetime
    tags: list[str] = []


class FeedResponse(BaseModel):
    items: list[SubscribeFeedItem]
    cursor: str          # ISO timestamp — pass as last_cursor on next poll
    count: int


@router.post("/", status_code=201)
async def subscribe(
    req: SubscribeRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    await check_namespace_access(key_entry, req.namespace)
    sub_id = await client.subscribe(
        user_id, req.namespace, req.filter_types,
        delivery_namespace=req.delivery_namespace,
        delivery_mode=req.delivery_mode,
        webhook_url=req.webhook_url,
    )
    result = {"subscribed": True, "namespace": req.namespace, "subscriber_id": user_id, "delivery_mode": req.delivery_mode}
    if req.delivery_namespace:
        result["delivery_namespace"] = req.delivery_namespace
        result["fan_out"] = True
    if req.webhook_url:
        result["webhook_url"] = req.webhook_url
    return result


@router.get("/{ns}/feed", response_model=FeedResponse)
async def get_feed(
    ns: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> FeedResponse:
    await check_namespace_access(key_entry, ns)
    memories, cursor = await client.get_feed(user_id, ns, limit)
    items = [
        SubscribeFeedItem(
            id=str(m.id),
            content=m.content,
            namespace=m.namespace,
            memory_type=m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type),
            author=m.author,
            created_at=m.created_at,
            tags=list(m.tags or []),
        )
        for m in memories
    ]
    return FeedResponse(items=items, cursor=cursor, count=len(items))


@router.get("/{ns}/stream")
async def stream_namespace(
    ns: str,
    subscriber_id: str = Query(..., description="Your subscriber ID"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    """SSE push stream for delivery_mode=immediate subscribers.

    Keeps a persistent HTTP connection open. Each new memory written to *ns*
    (or a child namespace) is pushed as a ``data:`` event with JSON payload.

    Only subscribers registered with ``delivery_mode=immediate`` should use
    this endpoint. The stream is in-process only — for multi-process deployments
    use ``delivery_mode=webhook`` instead.
    """
    import asyncio
    import json

    await check_namespace_access(key_entry, ns)

    try:
        from sse_starlette.sse import EventSourceResponse  # type: ignore
    except ImportError:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=501,
            content={"detail": "sse_starlette not installed — immediate delivery unavailable"},
        )

    from engram.subscription_bus import register as _register, unregister as _unregister

    # Fetch filter_types from the stored subscription record (best-effort)
    filter_types: list[str] = []
    try:
        sub = await client._arcadedb.get_subscription(subscriber_id, ns)
        if sub is not None:
            filter_types = list(getattr(sub, "filter_types", []) or [])
    except Exception:
        pass

    queue = _register(subscriber_id, ns, filter_types)
    logger.debug("SSE stream opened: subscriber=%s namespace=%s", subscriber_id, ns)

    async def _gen():
        try:
            yield {"event": "connected", "data": json.dumps({"subscriber_id": subscriber_id, "namespace": ns})}
            while True:
                try:
                    event = queue.get_nowait()
                    yield {"event": "memory.created", "data": json.dumps(event)}
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.1)
        finally:
            _unregister(subscriber_id, ns)
            logger.debug("SSE stream closed: subscriber=%s namespace=%s", subscriber_id, ns)

    return EventSourceResponse(_gen())


@router.delete("/{ns}", status_code=204, response_model=None)
async def unsubscribe(
    ns: str,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> None:
    await check_namespace_access(key_entry, ns, operation="write")
    await client.unsubscribe(user_id, ns)
