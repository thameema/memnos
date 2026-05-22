"""
engram_api.schemas — Pydantic v2 request/response models for the REST API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MemoryWriteRequest(BaseModel):
    content: str
    namespace: str
    tags: list[str] = []
    source: str = "user"
    metadata: dict[str, Any] = {}


class MemorySearchRequest(BaseModel):
    query: str
    namespace: str
    top_k: int = 10
    mode: str = "hybrid"  # hybrid | vector | graph


class MemoryResponse(BaseModel):
    id: str
    content: str
    namespace: str
    created_at: datetime
    tags: list[str]
    score: float | None = None


class GraphQueryRequest(BaseModel):
    cypher: str
    namespace: str
    params: dict[str, Any] = {}


class FactRequest(BaseModel):
    subject: str
    predicate: str
    object: str
    namespace: str
    valid_until: datetime | None = None


class SpawnTaskRequest(BaseModel):
    prompt: str
    namespace: str
    runtime: str = "api"
    agent: str | None = None
    timeout_s: int = 300


class TaskResponse(BaseModel):
    task_id: str
    status: str
    prompt: str | None = None
    result: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class FeedbackRequest(BaseModel):
    task_id: str
    signal: str  # "positive" | "negative"
    comment: str = ""
    namespace: str = "personal:default"


class HealthResponse(BaseModel):
    status: str
    neo4j: str
    qdrant: str
    version: str = "0.1.0"


class NamespaceCreateRequest(BaseModel):
    name: str
    owners: list[str] = []
    readers: list[str] = []
    writers: list[str] = []
