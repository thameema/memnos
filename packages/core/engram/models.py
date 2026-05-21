"""
engram.models — Core Pydantic v2 data models.

All IDs are uuid4 strings generated at construction time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Primary memory unit
# ---------------------------------------------------------------------------

class MemoryEntry(BaseModel):
    """A single recorded memory — stored in both Qdrant and Graphiti."""

    id: str = Field(default_factory=_uuid)
    content: str
    namespace: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    tags: list[str] = Field(default_factory=list)
    source: str = "agent"
    embedding_id: str | None = None
    graph_node_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Knowledge-graph primitives
# ---------------------------------------------------------------------------

class Entity(BaseModel):
    """A named entity extracted into the knowledge graph."""

    id: str = Field(default_factory=_uuid)
    name: str
    entity_type: str
    namespace: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    valid_until: datetime | None = None

    model_config = {"arbitrary_types_allowed": True}


class Relation(BaseModel):
    """A directed relation between two entities."""

    id: str = Field(default_factory=_uuid)
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    namespace: str
    weight: float = 1.0
    created_at: datetime = Field(default_factory=_now)
    valid_until: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class Fact(BaseModel):
    """A subject-predicate-object triple with optional temporal validity."""

    id: str = Field(default_factory=_uuid)
    subject: str
    predicate: str
    object: str
    namespace: str
    valid_from: datetime = Field(default_factory=_now)
    valid_until: datetime | None = None
    source_memory_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class Graph(BaseModel):
    """A sub-graph returned by traversal queries."""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Search output
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    """A ranked memory result from vector, graph, or hybrid search."""

    memory: MemoryEntry
    score: float
    source: str  # "vector" | "graph" | "hybrid"


# ---------------------------------------------------------------------------
# Namespace access control
# ---------------------------------------------------------------------------

class Namespace(BaseModel):
    """Access-control metadata for a namespace."""

    name: str
    owner_ids: list[str] = Field(default_factory=list)
    reader_ids: list[str] = Field(default_factory=list)
    writer_ids: list[str] = Field(default_factory=list)
