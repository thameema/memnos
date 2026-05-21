"""
engram_orchestrator.task_store — SQLite-backed async task store using aiosqlite.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import SubTask, Task, TaskStatus

logger = logging.getLogger(__name__)

_DT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime(_DT_FORMAT)


def _str_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        return datetime.strptime(s, _DT_FORMAT)
    except ValueError:
        # Fallback for timestamps without microseconds
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def _row_to_task(row: dict[str, Any]) -> Task:
    subtasks_json = row.get("subtasks_json") or "[]"
    subtasks_raw = json.loads(subtasks_json)
    subtasks = [_dict_to_subtask(st) for st in subtasks_raw]

    tags_json = row.get("tags_json") or "[]"
    tags = json.loads(tags_json)

    return Task(
        id=row["id"],
        prompt=row["prompt"],
        namespace=row["namespace"],
        runtime=row["runtime"],
        agent=row.get("agent"),
        status=TaskStatus(row["status"]),
        subtasks=subtasks,
        result=row.get("result"),
        error=row.get("error"),
        token_cost=row.get("token_cost") or 0,
        created_at=_str_to_dt(row.get("created_at")) or datetime.utcnow(),
        completed_at=_str_to_dt(row.get("completed_at")),
        parent_task_id=row.get("parent_task_id"),
        tags=tags,
    )


def _dict_to_subtask(d: dict[str, Any]) -> SubTask:
    return SubTask(
        id=d["id"],
        parent_task_id=d.get("parent_task_id", ""),
        prompt=d.get("prompt", ""),
        agent=d.get("agent"),
        worker_id=d.get("worker_id"),
        status=TaskStatus(d.get("status", TaskStatus.PENDING)),
        result=d.get("result"),
        error=d.get("error"),
        started_at=_str_to_dt(d.get("started_at")),
        completed_at=_str_to_dt(d.get("completed_at")),
    )


def _subtask_to_dict(st: SubTask) -> dict[str, Any]:
    return {
        "id": st.id,
        "parent_task_id": st.parent_task_id,
        "prompt": st.prompt,
        "agent": st.agent,
        "worker_id": st.worker_id,
        "status": st.status.value,
        "result": st.result,
        "error": st.error,
        "started_at": _dt_to_str(st.started_at),
        "completed_at": _dt_to_str(st.completed_at),
    }


class TaskStore:
    """SQLite-backed async store for Tasks and SubTasks."""

    def __init__(self, db_path: str = "~/.engram/tasks.db") -> None:
        expanded = Path(db_path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(expanded)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open the database and create tables if they don't exist."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()
        logger.debug("TaskStore initialised at %s", self._db_path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id              TEXT PRIMARY KEY,
                prompt          TEXT NOT NULL,
                namespace       TEXT NOT NULL,
                runtime         TEXT NOT NULL DEFAULT 'api',
                agent           TEXT,
                status          TEXT NOT NULL DEFAULT 'PENDING',
                result          TEXT,
                error           TEXT,
                token_cost      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT,
                completed_at    TEXT,
                parent_task_id  TEXT,
                tags_json       TEXT NOT NULL DEFAULT '[]',
                subtasks_json   TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_namespace ON tasks (namespace);
            CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks (status);

            CREATE TABLE IF NOT EXISTS subtasks (
                id              TEXT PRIMARY KEY,
                parent_task_id  TEXT NOT NULL,
                prompt          TEXT NOT NULL,
                agent           TEXT,
                worker_id       TEXT,
                status          TEXT NOT NULL DEFAULT 'PENDING',
                result          TEXT,
                error           TEXT,
                started_at      TEXT,
                completed_at    TEXT,
                FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_subtasks_parent ON subtasks (parent_task_id);
            """
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Task operations
    # ------------------------------------------------------------------

    async def save(self, task: Task) -> None:
        """Insert or replace a task."""
        assert self._db is not None
        subtasks_json = json.dumps([_subtask_to_dict(st) for st in task.subtasks])
        tags_json = json.dumps(task.tags)
        await self._db.execute(
            """
            INSERT OR REPLACE INTO tasks
                (id, prompt, namespace, runtime, agent, status, result, error,
                 token_cost, created_at, completed_at, parent_task_id, tags_json, subtasks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.prompt,
                task.namespace,
                task.runtime,
                task.agent,
                task.status.value,
                task.result,
                task.error,
                task.token_cost,
                _dt_to_str(task.created_at),
                _dt_to_str(task.completed_at),
                task.parent_task_id,
                tags_json,
                subtasks_json,
            ),
        )
        await self._db.commit()

    async def get(self, task_id: str) -> Task | None:
        """Retrieve a task by ID."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_task(dict(row))

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update a task's status and optionally its result/error."""
        assert self._db is not None
        completed_at = (
            _dt_to_str(datetime.utcnow())
            if status in (TaskStatus.COMPLETE, TaskStatus.FAILED)
            else None
        )
        await self._db.execute(
            """
            UPDATE tasks
            SET status       = ?,
                result       = COALESCE(?, result),
                error        = COALESCE(?, error),
                completed_at = COALESCE(?, completed_at)
            WHERE id = ?
            """,
            (status.value, result, error, completed_at, task_id),
        )
        await self._db.commit()

    async def list(
        self,
        namespace: str,
        status: str = "ALL",
        limit: int = 20,
    ) -> list[Task]:
        """List tasks for a namespace, optionally filtered by status."""
        assert self._db is not None
        if status == "ALL":
            query = (
                "SELECT * FROM tasks WHERE namespace = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple = (namespace, limit)
        else:
            query = (
                "SELECT * FROM tasks WHERE namespace = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (namespace, status, limit)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_task(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # SubTask operations
    # ------------------------------------------------------------------

    async def save_subtask(self, subtask: SubTask) -> None:
        """Insert or replace a subtask row."""
        assert self._db is not None
        await self._db.execute(
            """
            INSERT OR REPLACE INTO subtasks
                (id, parent_task_id, prompt, agent, worker_id, status,
                 result, error, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subtask.id,
                subtask.parent_task_id,
                subtask.prompt,
                subtask.agent,
                subtask.worker_id,
                subtask.status.value,
                subtask.result,
                subtask.error,
                _dt_to_str(subtask.started_at),
                _dt_to_str(subtask.completed_at),
            ),
        )
        await self._db.commit()

    async def update_subtask(
        self,
        subtask_id: str,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update a subtask's status, result, and timing fields."""
        assert self._db is not None
        now = _dt_to_str(datetime.utcnow())
        if status == TaskStatus.RUNNING:
            await self._db.execute(
                "UPDATE subtasks SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
                (status.value, now, subtask_id),
            )
        elif status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
            await self._db.execute(
                """
                UPDATE subtasks
                SET status       = ?,
                    result       = COALESCE(?, result),
                    error        = COALESCE(?, error),
                    completed_at = ?
                WHERE id = ?
                """,
                (status.value, result, error, now, subtask_id),
            )
        else:
            await self._db.execute(
                "UPDATE subtasks SET status = ? WHERE id = ?",
                (status.value, subtask_id),
            )
        await self._db.commit()

    async def get_subtasks(self, parent_task_id: str) -> list[SubTask]:
        """Return all subtasks for a parent task."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM subtasks WHERE parent_task_id = ? ORDER BY rowid",
            (parent_task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_dict_to_subtask(dict(r)) for r in rows]
