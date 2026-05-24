"""
engram_api.routers.subscriptions — Namespace pub-sub endpoints.

Endpoints
---------
POST   /subscriptions/              — subscribe to a namespace
GET    /subscriptions/{ns}/feed     — poll for new memories since last seen
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


@router.delete("/{ns}", status_code=204, response_model=None)
async def unsubscribe(
    ns: str,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> None:
    await check_namespace_access(key_entry, ns, operation="write")
    await client.unsubscribe(user_id, ns)
