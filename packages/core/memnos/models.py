"""
memnos.models — Core Pydantic v2 data models (v0.2 — ArcadeDB backend).

All temporal fields use UTC. created_at is immutable; superseded_at is set
when a fact is replaced by a newer version (never deleted — history preserved).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# Feature 5 — default confidence per memory source. Higher = more trustworthy.
# Verbatim user statements are 1.0; extracted/derived content scales down.
_DEFAULT_CONFIDENCE_BY_SOURCE: dict[str, float] = {
    "user":     1.00,   # human typed it directly
    "api":      1.00,   # explicit POST /memory/ — caller asserts truth
    "webhook":  0.80,   # external system push — trust but verify
    "file":     0.85,   # OCR/parsing may introduce small errors
    "voice":    0.75,   # ASR has higher error rates
    "agent":    0.70,   # LLM-derived inference, may hallucinate
    "import":   0.85,   # bulk import from another store
}


def default_confidence_for_source(source: str | None) -> float:
    """Confidence floor to apply when caller doesn't specify one."""
    if not source:
        return _DEFAULT_CONFIDENCE_BY_SOURCE["api"]
    return _DEFAULT_CONFIDENCE_BY_SOURCE.get(source.lower(), 0.7)


# ---------------------------------------------------------------------------
# Typed memory enumerations (Tier 1 — enterprise team features)
# ---------------------------------------------------------------------------

class DecayPolicy(str, Enum):
    """Controls how memory relevance scores decay over time.

    none            — no decay (default); score stays constant
    time_weighted   — score decays with age; half-life 90 days
    access_weighted — score decays with time since last access; half-life 30 days
    """
    none = "none"
    time_weighted = "time_weighted"
    access_weighted = "access_weighted"


class MemoryType(str, Enum):
    """Semantic classification of a memory entry.

    fact       — default; unstructured observation or note
    decision   — an architectural or technical decision with rationale
    constraint — a rule that AI agents must always respect (injected before search results)
    incident   — a production incident record with RCA
    adr        — Architecture Decision Record (structured: context/decision/consequences)
    skill      — a technique or capability tip, used by the Skill Coach
    """
    fact = "fact"
    decision = "decision"
    constraint = "constraint"
    incident = "incident"
    adr = "adr"
    skill = "skill"


class MemoryStatus(str, Enum):
    """Lifecycle status of a typed memory.

    active     — currently valid and enforced
    proposed   — under discussion, not yet enforced
    superseded — replaced by a newer decision (preserved for history)
    deprecated — intentionally retired
    """
    active = "active"
    proposed = "proposed"
    superseded = "superseded"
    deprecated = "deprecated"


# ---------------------------------------------------------------------------
# Provenance (Feature 2.2)
# ---------------------------------------------------------------------------

class Provenance(BaseModel):
    """Chain of custody for a memory — who, what tool, which commit, which ticket."""
    agent_id: str = ""        # e.g. "claude-code-session-abc123"
    user_id: str = ""         # person who triggered this write
    tool: str = ""            # "claude-code" | "memnos-git" | "api" | "mcp"
    git_commit: str = ""      # short SHA if written from a git hook
    jira_ticket: str = ""     # e.g. "HPTE-242"
    team: str = ""            # team name or department


# ---------------------------------------------------------------------------
# Episode — immutable raw input (Feature 2: source of ground truth)
# ---------------------------------------------------------------------------

class Episode(BaseModel):
    """An immutable verbatim record of an input that produced one or more Memories.

    Episodes are write-only through the public API — no PATCH, no DELETE.
    Every Memory carries `source_episode_ids` linking back to the Episode(s)
    it was derived from, so:

      • Audit / compliance can always re-trace any Memory to its raw source
      • Future extraction logic can re-derive Memories from the original
        Episode content (eg. when a better LLM or extractor is released)
      • PHI / PII redaction can be revisited against the original input

    For audit-driven verticals (healthcare, life sciences, legal, defense)
    this is the foundational primitive — every derived fact has an
    immutable provenance chain.
    """
    id: str = Field(default_factory=_uuid)
    content: str                                  # exact verbatim text as submitted
    namespace: str
    created_at: datetime = Field(default_factory=_now)
    source: str = "api"                           # api | file | voice | webhook | mcp
    author: str = ""                              # user_id from the api_key that wrote it
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: "Provenance" = Field(default_factory=lambda: Provenance())

    model_config = {"arbitrary_types_allowed": True}


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

    # Tier 1 — typed memory fields
    memory_type: MemoryType = MemoryType.fact
    status: MemoryStatus = MemoryStatus.active
    author: str = ""                        # who recorded this (user_id or team name)
    affects: list[str] = Field(default_factory=list)  # entity names this memory governs
    rationale: str = ""                     # WHY — the reasoning behind a decision/constraint
    expires_at: datetime | None = None      # hard expiry; expired memories excluded from search
    review_by: datetime | None = None       # soft flag: surface for human review after this date

    provenance: "Provenance" = Field(default_factory=lambda: Provenance())
    decay_policy: "DecayPolicy" = DecayPolicy.none
    last_accessed_at: datetime | None = None   # updated on every search hit

    # Feature 2 — immutable source-episode lineage. Every Memory derived
    # from a raw input carries the Episode id(s) it was extracted from,
    # so audit / re-extraction / PII review can always trace back to source.
    source_episode_ids: list[str] = Field(default_factory=list)

    # Feature 5 — confidence in [0.0, 1.0]. Lets downstream callers filter
    # noisy/derived memories. Defaults set per source via
    # ``default_confidence_for_source`` when not supplied.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    @computed_field
    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Namespace Subscriptions (Feature 2.1)
# ---------------------------------------------------------------------------

class Subscription(BaseModel):
    """A subscription: subscriber polls namespace for new memories since last_seen."""
    id: str = Field(default_factory=_uuid)
    subscriber_id: str            # user_id or agent_id of the subscriber
    namespace: str                # source namespace being watched
    filter_types: list[str] = Field(default_factory=list)  # [] = all types
    delivery_namespace: str = ""  # if set, new memories are auto-copied here (fan-out push)
    delivery_mode: str = "cursor"  # "cursor" | "webhook" | "immediate"
    webhook_url: str = ""          # HTTPS endpoint for webhook delivery (delivery_mode=webhook)
    last_seen_at: datetime = Field(default_factory=_now)   # high-water mark
    created_at: datetime = Field(default_factory=_now)
    active: bool = True


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


class Community(BaseModel):
    """A detected cluster of entities produced by community detection."""

    id: str = Field(default_factory=_uuid)    # overridden with hash on creation
    label: str
    namespace: str
    member_names: list[str] = Field(default_factory=list)
    member_count: int = 0
    detected_at: datetime = Field(default_factory=_now)
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
# Corpus — architecture doc ingestion source
# ---------------------------------------------------------------------------

class Corpus(BaseModel):
    """A registered corpus source: a directory (or git repo path) of docs ingested
    into memnos as typed constraint/decision/fact nodes.

    ``status`` lifecycle: pending → syncing → ready | error
    ``watch=True`` enables auto-sync via the GitLab CI webhook endpoint.
    """

    id: str = Field(default_factory=_uuid)
    name: str                                    # human label e.g. "hdig-platform-architecture"
    source_path: str                             # local path or git-cloned directory
    path_pattern: str = "**/*.md"               # glob relative to source_path
    namespace: str                               # target memnos namespace
    watch: bool = False                          # re-sync on webhook push
    webhook_secret: str = ""                     # HMAC secret for GitLab CI webhook
    last_sync_sha: str = ""                      # git SHA of last successful sync
    last_sync_at: datetime | None = None
    node_count: int = 0                          # memory nodes written in last sync
    status: str = "pending"                      # pending | syncing | ready | error
    error_msg: str = ""
    created_at: datetime = Field(default_factory=_now)
    created_by: str = ""

    model_config = {"arbitrary_types_allowed": True}


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


class NamespaceType(str, Enum):
    """Feature 8 — separates operational memory from reference knowledge.

    work       — operational memories (engineering notes, decisions, agent
                 context). The default. Searches default to this scope.
    reference  — org knowledge base (standards like CMS-0057-F, HL7 IGs,
                 policies, RFCs, vendor docs). Material the agent should
                 CONSULT but never treat as work output. Excluded from the
                 default search to prevent cross-contamination.
    """
    work = "work"
    reference = "reference"


class Namespace(BaseModel):
    """Access-control + type metadata for a namespace."""

    name: str
    owner_ids: list[str] = Field(default_factory=list)
    reader_ids: list[str] = Field(default_factory=list)
    writer_ids: list[str] = Field(default_factory=list)
    # Feature 8 — namespace scope. Reference namespaces are excluded from
    # default search; callers must explicitly opt in via include_reference=true.
    namespace_type: NamespaceType = NamespaceType.work


# ---------------------------------------------------------------------------
# Vault — encrypted secrets
# ---------------------------------------------------------------------------

class Secret(BaseModel):
    """Encrypted secret stored in the vault.

    The plaintext value is NEVER stored.  ``value_enc`` and ``dek_enc`` are
    base64-encoded AES-256-GCM ciphertexts (envelope encryption).
    """

    id: str = Field(default_factory=_uuid)
    key_name: str                            # human-readable identifier, e.g. "openai_api_key"
    note: str = ""
    secret_type: str = "api_key"            # "api_key"|"token"|"password"|"certificate"|"webhook"|"other"
    namespace: str
    value_enc: str                           # base64 AES-256-GCM encrypted plaintext
    dek_enc: str                             # base64 AES-256-GCM encrypted DEK (wrapped by KEK)
    created_at: datetime = Field(default_factory=_now)
    superseded_at: datetime | None = None
    created_by: str = "unknown"
    tags: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def is_current(self) -> bool:
        return self.superseded_at is None

    model_config = {"arbitrary_types_allowed": True}


class VaultAuditLog(BaseModel):
    """Immutable audit record written on every vault access."""

    id: str = Field(default_factory=_uuid)
    secret_name: str
    namespace: str
    action: str                              # "get"|"set"|"rotate"|"delete"|"list"
    accessed_by: str                         # user_id from the API key
    accessed_at: datetime = Field(default_factory=_now)
    ok: bool = True
    err_msg: str | None = None

    model_config = {"arbitrary_types_allowed": True}
