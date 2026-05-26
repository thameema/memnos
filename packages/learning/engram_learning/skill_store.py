"""SQLite-backed skill template store."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from engram_learning.models import SkillTemplate

logger = logging.getLogger(__name__)
_DB_PATH = Path.home() / ".engram" / "learning.db"


class SkillStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or _DB_PATH)

    async def init(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS skill_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    namespace TEXT,
                    description TEXT,
                    trigger_patterns TEXT,
                    steps TEXT,
                    tools_used TEXT,
                    avg_duration_s REAL,
                    success_rate REAL,
                    source_episode_id TEXT,
                    created_at TEXT,
                    last_used_at TEXT,
                    use_count INTEGER
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS sk_ns ON skill_templates(namespace)")
            await db.commit()

    def _row(self, row) -> SkillTemplate:
        return SkillTemplate(
            id=row[0], name=row[1], namespace=row[2], description=row[3],
            trigger_patterns=json.loads(row[4] or "[]"),
            steps=json.loads(row[5] or "[]"),
            tools_used=json.loads(row[6] or "[]"),
            avg_duration_s=row[7] or 0.0,
            success_rate=row[8] or 1.0,
            source_episode_id=row[9] or "",
            created_at=datetime.fromisoformat(row[10]) if row[10] else datetime.now(timezone.utc),
            last_used_at=datetime.fromisoformat(row[11]) if row[11] else None,
            use_count=row[12] or 0,
        )

    async def add(self, t: SkillTemplate):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO skill_templates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t.id, t.name, t.namespace, t.description,
                    json.dumps(t.trigger_patterns), json.dumps(t.steps),
                    json.dumps(t.tools_used), t.avg_duration_s, t.success_rate,
                    t.source_episode_id, t.created_at.isoformat(),
                    t.last_used_at.isoformat() if t.last_used_at else None,
                    t.use_count,
                ),
            )
            await db.commit()

    async def get_all(self, namespace: str) -> list[SkillTemplate]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM skill_templates WHERE namespace=? ORDER BY use_count DESC",
                (namespace,),
            ) as cur:
                return [self._row(r) for r in await cur.fetchall()]

    async def find_match(self, task: str, namespace: str, threshold: float = 0.7) -> SkillTemplate | None:
        """Find the best matching skill template for a task using trigger pattern scoring.

        Each template's score is the fraction of its ``trigger_patterns`` that
        appear (case-insensitive substring match) in *task*.  The template with
        the highest score is returned when that score meets *threshold*.
        """
        templates = await self.get_all(namespace)
        task_lower = task.lower()

        best: SkillTemplate | None = None
        best_score = 0.0

        for template in templates:
            if not template.trigger_patterns:
                continue
            matched = sum(1 for p in template.trigger_patterns if p.lower() in task_lower)
            score = matched / len(template.trigger_patterns)
            if score > best_score:
                best_score = score
                best = template

        if best_score >= threshold:
            return best
        return None

    async def increment_use(self, template_id: str, success: bool):
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                await db.execute(
                    """UPDATE skill_templates
                       SET use_count=use_count+1,
                           last_used_at=?,
                           success_rate=((success_rate * use_count) + 1.0) / (use_count + 1)
                       WHERE id=?""",
                    (datetime.now(timezone.utc).isoformat(), template_id),
                )
            else:
                await db.execute(
                    """UPDATE skill_templates
                       SET use_count=use_count+1,
                           last_used_at=?,
                           success_rate=(success_rate * use_count) / (use_count + 1)
                       WHERE id=?""",
                    (datetime.now(timezone.utc).isoformat(), template_id),
                )
            await db.commit()

    async def delete(self, template_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM skill_templates WHERE id=?", (template_id,))
            await db.commit()
