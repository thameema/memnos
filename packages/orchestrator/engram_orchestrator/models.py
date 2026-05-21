"""
engram_orchestrator.models — Task and SubTask data models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import uuid4


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    SYNTHESIZING = "SYNTHESIZING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass
class SubTask:
    id: str = field(default_factory=lambda: str(uuid4()))
    parent_task_id: str = ""
    prompt: str = ""
    agent: str | None = None
    worker_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid4()))
    prompt: str = ""
    namespace: str = ""
    runtime: str = "api"
    agent: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    subtasks: list[SubTask] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    token_cost: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    parent_task_id: str | None = None
    tags: list[str] = field(default_factory=list)
