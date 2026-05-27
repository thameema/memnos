"""
engram.corpus.store — SQLite-backed registry of ingested corpus sources.

Each Corpus record tracks a directory or git repo that has been ingested into
engram as structured constraint/decision/fact nodes.  The store is intentionally
separate from ArcadeDB — corpus metadata is operational (sync state, node counts)
and does not need to be searchable as memories.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from engram.models import Corpus

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.environ.get(
    "ENGRAM_CORPUS_DB",
    str(Path.home() / ".engram" / "corpus.db"),
)

_DDL = """
CREATE TABLE IF NOT EXISTS corpus (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    path_pattern  TEXT NOT NULL DEFAULT '**/*.md',
    namespace     TEXT NOT NULL,
    watch         INTEGER NOT NULL DEFAULT 0,
    webhook_secret TEXT NOT NULL DEFAULT '',
    last_sync_sha  TEXT NOT NULL DEFAULT '',
    last_sync_at   TEXT,
    node_count     INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending',
    error_msg      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL DEFAULT ''
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_corpus(row: dict[str, Any]) -> Corpus:
    return Corpus(
        id=row["id"],
        name=row["name"],
        source_path=row["source_path"],
        path_pattern=row["path_pattern"],
        namespace=row["namespace"],
        watch=bool(row["watch"]),
        webhook_secret=row.get("webhook_secret", ""),
        last_sync_sha=row.get("last_sync_sha", ""),
        last_sync_at=datetime.fromisoformat(row["last_sync_at"]) if row.get("last_sync_at") else None,
        node_count=row.get("node_count", 0),
        status=row.get("status", "pending"),
        error_msg=row.get("error_msg", ""),
        created_at=datetime.fromisoformat(row["created_at"]),
        created_by=row.get("created_by", ""),
    )


class CorpusStore:
    """Async SQLite store for corpus source registrations."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_DDL)
            await db.commit()
        logger.debug("CorpusStore initialised at %s", self._db_path)

    async def create(self, corpus: Corpus) -> Corpus:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO corpus
                   (id, name, source_path, path_pattern, namespace, watch,
                    webhook_secret, last_sync_sha, last_sync_at, node_count,
                    status, error_msg, created_at, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    corpus.id, corpus.name, corpus.source_path, corpus.path_pattern,
                    corpus.namespace, int(corpus.watch), corpus.webhook_secret,
                    corpus.last_sync_sha,
                    corpus.last_sync_at.isoformat() if corpus.last_sync_at else None,
                    corpus.node_count, corpus.status, corpus.error_msg,
                    corpus.created_at.isoformat(), corpus.created_by,
                ),
            )
            await db.commit()
        return corpus

    async def get(self, corpus_id: str) -> Corpus | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM corpus WHERE id = ?", (corpus_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_corpus(dict(row)) if row else None

    async def list_all(self) -> list[Corpus]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM corpus ORDER BY created_at DESC") as cur:
                rows = await cur.fetchall()
        return [_row_to_corpus(dict(r)) for r in rows]

    async def update_sync_state(
        self,
        corpus_id: str,
        *,
        status: str,
        node_count: int = 0,
        last_sync_sha: str = "",
        error_msg: str = "",
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """UPDATE corpus
                   SET status=?, node_count=?, last_sync_sha=?,
                       last_sync_at=?, error_msg=?
                   WHERE id=?""",
                (
                    status, node_count, last_sync_sha,
                    _now_iso(), error_msg, corpus_id,
                ),
            )
            await db.commit()

    async def delete(self, corpus_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM corpus WHERE id = ?", (corpus_id,))
            await db.commit()
            return cur.rowcount > 0
