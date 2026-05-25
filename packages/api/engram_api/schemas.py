"""
engram_api.schemas — Pydantic v2 request/response models for the REST API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProvenanceInput(BaseModel):
    agent_id: str = ""
    user_id: str = ""
    tool: str = ""
    git_commit: str = ""
    jira_ticket: str = ""
    team: str = ""


class MemoryWriteRequest(BaseModel):
    content: str
    namespace: str
    tags: list[str] = []
    source: str = "user"
    metadata: dict[str, Any] = {}
    memory_type: str = "fact"
    status: str = "active"
    author: str = ""
    affects: list[str] = []
    rationale: str = ""
    provenance: ProvenanceInput = Field(default_factory=ProvenanceInput)
    expires_at: datetime | None = None
    review_by: datetime | None = None


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
    memory_type: str = "fact"
    author: str = ""
    provenance: dict = Field(default_factory=dict)
    contradiction_warnings: list[dict] = Field(default_factory=list)


class ReviewDueItem(BaseModel):
    id: str
    content: str
    namespace: str
    memory_type: str
    author: str
    review_by: datetime
    created_at: datetime
    tags: list[str] = []
    rationale: str = ""


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
    arcadedb: str
    version: str = "0.2.0"


class NamespaceCreateRequest(BaseModel):
    name: str
    owners: list[str] = []
    readers: list[str] = []
    writers: list[str] = []


# ---------------------------------------------------------------------------
# Knowledge Q&A schemas
# ---------------------------------------------------------------------------

class KnowledgeAskRequest(BaseModel):
    question: str
    namespace: str
    top_k: int = 5
    model: str = "claude-haiku-4-5-20251001"


class KnowledgeAnswerResponse(BaseModel):
    answer: str
    sources: list[MemoryResponse]
    namespace: str
    model_used: str
    tokens_used: int


# ---------------------------------------------------------------------------
# Runtime key management schemas
# ---------------------------------------------------------------------------

class KeyCreateRequest(BaseModel):
    user_id: str
    namespaces: list[str] = ["*"]
    read_only: bool = False
    description: str = ""


class KeyResponse(BaseModel):
    id: str
    key_prefix: str
    user_id: str
    namespaces: list[str]
    read_only: bool
    description: str
    created_at: str
    revoked_at: str | None = None
    # Only populated on initial creation — never returned again after that.
    key: str | None = None
