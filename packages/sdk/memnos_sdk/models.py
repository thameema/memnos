from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    ADR = "adr"
    SESSION = "session"
    EPISODE = "episode"


# ---------------------------------------------------------------------------
# Corpus models
# ---------------------------------------------------------------------------

class CorpusStatus(str, Enum):
    PENDING  = "pending"
    SYNCING  = "syncing"
    READY    = "ready"
    ERROR    = "error"


class CorpusInfo(BaseModel):
    """Metadata for a registered corpus source."""
    id: str
    name: str
    source_path: str
    path_pattern: str
    namespace: str
    connector_type: str = "git-doc"
    watch: bool
    status: CorpusStatus
    node_count: int
    last_sync_sha: str
    last_sync_at: datetime | None = None
    error_msg: str = ""
    created_at: datetime
    created_by: str = ""


class ConstraintHit(BaseModel):
    """A single constraint node returned by a corpus check."""
    memory_id: str
    content: str
    severity: str       # "SHALL" | "SHOULD" | "MAY" | ""
    source_file: str
    section: str
    score: float


class CheckResult(BaseModel):
    """Result of a corpus constraint check against a code snippet."""
    corpus_id: str
    namespace: str
    constraints: list[ConstraintHit]

    @property
    def shall_violations(self) -> list[ConstraintHit]:
        """Constraints with SHALL severity — highest priority."""
        return [c for c in self.constraints if c.severity == "SHALL"]

    @property
    def should_violations(self) -> list[ConstraintHit]:
        """Constraints with SHOULD severity."""
        return [c for c in self.constraints if c.severity == "SHOULD"]

    def format(self) -> str:
        """Human-readable summary for agent prompts."""
        if not self.constraints:
            return f"No constraints found for corpus {self.corpus_id}."
        lines = [f"Corpus: {self.corpus_id} | Namespace: {self.namespace}"]
        lines.append(f"Found {len(self.constraints)} relevant constraint(s):\n")
        for i, c in enumerate(self.constraints, 1):
            sev = f"[{c.severity}] " if c.severity else ""
            lines.append(f"{i}. {sev}{c.content}")
            if c.source_file or c.section:
                src = c.source_file
                lines.append(f"   Source: {src}" + (f" | Section: {c.section}" if c.section else ""))
            lines.append(f"   Score: {c.score:.3f}")
            lines.append("")
        return "\n".join(lines)


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
