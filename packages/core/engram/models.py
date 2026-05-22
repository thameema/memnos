"""
engram.models — Core Pydantic v2 data models (v0.2 — ArcadeDB backend).

All temporal fields use UTC. created_at is immutable; superseded_at is set
when a fact is replaced by a newer version (never deleted — history preserved).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, computed_field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Primary memory unit
# ---------------------------------------------------------------------------

class MemoryEntry(BaseModel):
    """A single recorded memory stored in ArcadeDB."""

    id: str = Field(default_factory=_uuid)
    content: str
    namespace: str
    created_at: datetime = Field(default_factory=_now)
    superseded_at: datetime | None = None   # None = currently valid
    tags: list[str] = Field(default_factory=list)
    source: str = "agent"                   # "user" | "agent" | "file" | "api"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Knowledge-graph primitives
# ---------------------------------------------------------------------------

class Entity(BaseModel):
    """A named entity extracted from memories via spaCy."""

    id: str = Field(default_factory=_uuid)
    name: str                               # normalized lowercase
    entity_type: str                        # "PERSON"|"ORG"|"TECH"|"DECISION"|"CONCEPT"
    namespace: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    superseded_at: datetime | None = None

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    model_config = {"arbitrary_types_allowed": True}


class Relation(BaseModel):
    """A directed relation between two entities."""

    id: str = Field(default_factory=_uuid)
    source_entity_id: str
    target_entity_id: str
    relation_type: str                      # "USES"|"DECIDED"|"DEPENDS_ON"|"SUPERSEDES"
    namespace: str
    weight: float = 1.0
    created_at: datetime = Field(default_factory=_now)
    superseded_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class Fact(BaseModel):
    """A subject-predicate-object triple — explicit assertion about the world."""

    id: str = Field(default_factory=_uuid)
    subject: str                            # entity name
    predicate: str                          # e.g. "uses", "decided", "requires"
    object: str                             # entity name or literal
    namespace: str
    created_at: datetime = Field(default_factory=_now)   # when this became true
    superseded_at: datetime | None = None                 # when it was replaced
    source_memory_id: str | None = None

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    model_config = {"arbitrary_types_allowed": True}


class Graph(BaseModel):
    """A sub-graph returned by traversal queries."""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Binary asset reference
# ---------------------------------------------------------------------------

class AssetReference(BaseModel):
    """Pointer to a binary file — never stores the file itself."""

    id: str = Field(default_factory=_uuid)
    path: str                               # local path or git URL
    format: str                             # "drawio"|"pdf"|"png"|"docx"|"svg"|...
    sha256: str                             # content hash for change detection
    extracted_content: str = ""            # text extracted from the binary
    namespace: str
    created_at: datetime = Field(default_factory=_now)
    superseded_at: datetime | None = None   # set when file hash changes
    created_by: str = "agent"
    related_memory_ids: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Search output
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    """A ranked memory result from vector, graph, or hybrid search."""

    memory: MemoryEntry
    score: float
    source: str                             # "vector" | "graph" | "hybrid"
    is_current: bool = True                 # False = [HISTORICAL]
    recency_score: float = 1.0


# ---------------------------------------------------------------------------
# Namespace access control
# ---------------------------------------------------------------------------

class NamespaceAccess(BaseModel):
    """Access entry: one namespace + the permission level for a key."""
    namespace: str
    access: str = "read_write"              # "read_only" | "read_write"


class Namespace(BaseModel):
    """Access-control metadata for a namespace."""

    name: str
    owner_ids: list[str] = Field(default_factory=list)
    reader_ids: list[str] = Field(default_factory=list)
    writer_ids: list[str] = Field(default_factory=list)
