from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    ADR = "adr"
    SESSION = "session"
    EPISODE = "episode"


class Memory(BaseModel):
    id: str
    content: str
    namespace: str
    memory_type: MemoryType
    tags: list[str]
    affects: list[str]
    rationale: str
    author: str
    created_at: datetime
    score: float | None = None
    provenance: dict = Field(default_factory=dict)
    contradiction_warnings: list[dict] = Field(default_factory=list)


class SearchResult(BaseModel):
    memories: list[Memory]
    # constraints are returned as Memory objects with memory_type=constraint and score=2.0
    # They are already in the memories list; this model is transparent about that


class HealthStatus(BaseModel):
    status: str
    arcadedb: str
    version: str
    schema_version: str = "1.0"
