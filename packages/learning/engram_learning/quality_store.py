"""Quality record store — tracks per-agent, per-topic performance."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from engram_learning.models import QualityRecord

logger = logging.getLogger(__name__)
_DB_PATH = Path.home() / ".engram" / "learning.db"


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


class QualityStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or _DB_PATH)

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS quality_records (
                    agent_name TEXT,
                    task_tag TEXT,
                    namespace TEXT,
                    sample_count INTEGER,
                    avg_quality_score REAL,
                    avg_duration_s REAL,
                    failure_rate REAL,
                    last_updated INTEGER,
                    PRIMARY KEY (agent_name, task_tag, namespace)
                )
            """)
            await db.commit()

    def _row(self, row) -> QualityRecord:
        return QualityRecord(
            agent_name=row[0], task_tag=row[1], namespace=row[2],
            sample_count=row[3] or 0, avg_quality_score=row[4] or 0.0,
            avg_duration_s=row[5] or 0.0, failure_rate=row[6] or 0.0,
            last_updated=_from_ms(row[7]) or datetime.now(timezone.utc),
        )

    async def update(
        self,
        agent_name: str,
        task_tag: str,
        namespace: str,
        quality_score: float,
        duration_s: float,
        success: bool,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM quality_records WHERE agent_name=? AND task_tag=? AND namespace=?",
                (agent_name, task_tag, namespace),
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                n = existing[3]
                new_n = n + 1
                new_avg_q = (existing[4] * n + quality_score) / new_n
                new_avg_d = (existing[5] * n + duration_s) / new_n
                failures = existing[6] * n + (0 if success else 1)
                new_fail = failures / new_n
                await db.execute(
                    """UPDATE quality_records
                       SET sample_count=?, avg_quality_score=?, avg_duration_s=?,
                           failure_rate=?, last_updated=?
                       WHERE agent_name=? AND task_tag=? AND namespace=?""",
                    (new_n, new_avg_q, new_avg_d, new_fail, _now_ms(),
                     agent_name, task_tag, namespace),
                )
            else:
                await db.execute(
                    "INSERT INTO quality_records VALUES (?,?,?,?,?,?,?,?)",
                    (agent_name, task_tag, namespace, 1, quality_score, duration_s,
                     0.0 if success else 1.0, _now_ms()),
                )
            await db.commit()

    async def get(self, task_tag: str, namespace: str) -> list[QualityRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM quality_records WHERE task_tag=? AND namespace=? ORDER BY avg_quality_score DESC",
                (task_tag, namespace),
            ) as cur:
                return [self._row(r) for r in await cur.fetchall()]

    async def get_best_agent(self, task_tag: str, namespace: str, min_samples: int = 10) -> str | None:
        records = await self.get(task_tag, namespace)
        eligible = [r for r in records if r.sample_count >= min_samples]
        if not eligible:
            return None
        best = max(eligible, key=lambda r: r.avg_quality_score - 2 * r.failure_rate)
        return best.agent_name if best.avg_quality_score > 0.6 else None
