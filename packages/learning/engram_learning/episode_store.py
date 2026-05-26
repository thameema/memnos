"""SQLite-backed episodic memory store."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from engram_learning.models import EpisodicRecord, Outcome

logger = logging.getLogger(__name__)
_DB_PATH = Path.home() / ".engram" / "learning.db"


def _to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(int(val) / 1000, tz=timezone.utc)
    s = str(val).strip()
    if s.lstrip('-').isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class EpisodeStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or _DB_PATH)

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    namespace TEXT,
                    original_prompt TEXT,
                    decomposition TEXT,
                    agent_used TEXT,
                    runtime TEXT,
                    outcome TEXT,
                    user_feedback TEXT,
                    quality_score REAL,
                    duration_s REAL,
                    token_cost INTEGER,
                    created_at INTEGER,
                    tags TEXT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS ep_ns ON episodes(namespace)")
            await db.execute("CREATE INDEX IF NOT EXISTS ep_task ON episodes(task_id)")
            await db.commit()

    async def save(self, ep: EpisodicRecord):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ep.id, ep.task_id, ep.namespace, ep.original_prompt,
                    json.dumps(ep.decomposition), ep.agent_used, ep.runtime,
                    ep.outcome.value, ep.user_feedback, ep.quality_score,
                    ep.duration_s, ep.token_cost,
                    _to_ms(ep.created_at), json.dumps(ep.tags),
                ),
            )
            await db.commit()

    def _row_to_episode(self, row) -> EpisodicRecord:
        return EpisodicRecord(
            id=row[0], task_id=row[1], namespace=row[2], original_prompt=row[3],
            decomposition=json.loads(row[4] or "[]"), agent_used=row[5],
            runtime=row[6], outcome=Outcome(row[7]),
            user_feedback=row[8], quality_score=row[9],
            duration_s=row[10] or 0.0, token_cost=row[11] or 0,
            created_at=_from_ms(row[12]) or datetime.now(timezone.utc),
            tags=json.loads(row[13] or "[]"),
        )

    async def get(self, episode_id: str) -> EpisodicRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_episode(row) if row else None

    async def get_by_task_id(self, task_id: str) -> EpisodicRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT * FROM episodes WHERE task_id=? LIMIT 1", (task_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_episode(row) if row else None

    async def get_recent(self, namespace: str, days: int = 7) -> list[EpisodicRecord]:
        since_ms = _to_ms(datetime.now(timezone.utc) - timedelta(days=days))
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM episodes WHERE namespace=? AND created_at>=? ORDER BY created_at DESC",
                (namespace, since_ms),
            ) as cur:
                rows = await cur.fetchall()
                return [self._row_to_episode(r) for r in rows]

    async def get_active_namespaces(self, days: int = 7) -> list[str]:
        """Return distinct namespaces that have had episodes in the last *days* days."""
        since_ms = _to_ms(datetime.now(timezone.utc) - timedelta(days=days))
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT namespace FROM episodes WHERE created_at >= ?",
                (since_ms,),
            ) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows if r[0]]

    async def update_outcome(
        self,
        episode_id: str,
        outcome: Outcome,
        feedback: str | None = None,
        quality_score: float | None = None,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE episodes SET outcome=?, user_feedback=?, quality_score=? WHERE id=?",
                (outcome.value, feedback, quality_score, episode_id),
            )
            await db.commit()
