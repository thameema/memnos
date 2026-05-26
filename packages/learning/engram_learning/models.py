"""Learning data models — episodic records, heuristics, skill templates, quality records."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)
from enum import Enum
from uuid import uuid4


class Outcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CORRECTED = "CORRECTED"


@dataclass
class EpisodicRecord:
    id: str = field(default_factory=lambda: str(uuid4()))
    task_id: str = ""
    namespace: str = ""
    original_prompt: str = ""
    decomposition: list[str] = field(default_factory=list)
    agent_used: str | None = None
    runtime: str = "api"
    outcome: Outcome = Outcome.SUCCESS
    user_feedback: str | None = None
    quality_score: float | None = None
    duration_s: float = 0.0
    token_cost: int = 0
    created_at: datetime = field(default_factory=_now)
    tags: list[str] = field(default_factory=list)


@dataclass
class Heuristic:
    id: str = field(default_factory=lambda: str(uuid4()))
    namespace: str = ""
    rule: str = ""
    rationale: str = ""
    source_episode_id: str = ""
    applies_to_tags: list[str] = field(default_factory=list)
    confidence: float = 0.8
    triggered_count: int = 0
    overridden_count: int = 0
    created_at: datetime = field(default_factory=_now)
    last_triggered_at: datetime | None = None


@dataclass
class SkillTemplate:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    namespace: str = ""
    description: str = ""
    trigger_patterns: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    avg_duration_s: float = 0.0
    success_rate: float = 1.0
    source_episode_id: str = ""
    created_at: datetime = field(default_factory=_now)
    last_used_at: datetime | None = None
    use_count: int = 0


@dataclass
class QualityRecord:
    agent_name: str = ""
    task_tag: str = ""
    namespace: str = ""
    sample_count: int = 0
    avg_quality_score: float = 0.0
    avg_duration_s: float = 0.0
    failure_rate: float = 0.0
    last_updated: datetime = field(default_factory=_now)
