"""SQLite-backed heuristic store."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from engram_learning.models import Heuristic

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


class HeuristicStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or _DB_PATH)

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS heuristics (
                    id TEXT PRIMARY KEY,
                    namespace TEXT,
                    rule TEXT,
                    rationale TEXT,
                    source_episode_id TEXT,
                    applies_to_tags TEXT,
                    confidence REAL,
                    triggered_count INTEGER,
                    overridden_count INTEGER,
                    created_at INTEGER,
                    last_triggered_at INTEGER
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS h_ns ON heuristics(namespace)")
            await db.commit()

    def _row(self, row) -> Heuristic:
        return Heuristic(
            id=row[0], namespace=row[1], rule=row[2], rationale=row[3],
            source_episode_id=row[4],
            applies_to_tags=json.loads(row[5] or "[]"),
            confidence=row[6] or 0.8,
            triggered_count=row[7] or 0,
            overridden_count=row[8] or 0,
            created_at=_from_ms(row[9]) or datetime.now(timezone.utc),
            last_triggered_at=_from_ms(row[10]),
        )

    async def add(self, h: Heuristic):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO heuristics VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    h.id, h.namespace, h.rule, h.rationale, h.source_episode_id,
                    json.dumps(h.applies_to_tags), h.confidence,
                    h.triggered_count, h.overridden_count,
                    _to_ms(h.created_at),
                    _to_ms(h.last_triggered_at),
                ),
            )
            await db.commit()

    async def get_all(self, namespace: str) -> list[Heuristic]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM heuristics WHERE namespace=? ORDER BY confidence DESC",
                (namespace,),
            ) as cur:
                return [self._row(r) for r in await cur.fetchall()]

    async def search(self, namespace: str, query_tags: list[str] | None = None, limit: int = 20) -> list[Heuristic]:
        all_h = await self.get_all(namespace)
        if not query_tags:
            return all_h[:limit]
        scored = []
        for h in all_h:
            overlap = len(set(h.applies_to_tags) & set(query_tags))
            if overlap > 0 or not h.applies_to_tags:
                scored.append((overlap, h))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for _, h in scored[:limit]]

    async def update_confidence(self, heuristic_id: str, delta: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE heuristics SET confidence = MAX(0.0, MIN(1.0, confidence + ?)) WHERE id=?",
                (delta, heuristic_id),
            )
            await db.commit()

    async def increment_triggered(self, heuristic_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE heuristics SET triggered_count=triggered_count+1, last_triggered_at=? WHERE id=?",
                (_now_ms(), heuristic_id),
            )
            await db.commit()

    async def get_by_tags(self, namespace: str, tags: list[str]) -> list[Heuristic]:
        """Return heuristics whose applies_to_tags overlaps with *tags*."""
        return await self.search(namespace, query_tags=tags)

    async def delete(self, heuristic_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM heuristics WHERE id=?", (heuristic_id,))
            await db.commit()
