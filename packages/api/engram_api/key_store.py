"""
engram_api.key_store — Runtime API key store backed by SQLite.

Keys created via the admin API are stored here (hashed).  YAML keys are the
static "master" keys; this store holds user-managed keys alongside them.

Schema
------
keys (
    id          TEXT PRIMARY KEY,    -- UUID
    key_hash    TEXT UNIQUE,         -- SHA-256(raw_key) hex
    key_prefix  TEXT,                -- first 8 chars of raw_key (display only)
    user_id     TEXT,
    namespaces  TEXT,                -- JSON list
    read_only   INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    created_at  TEXT,
    revoked_at  TEXT                 -- NULL = active
)
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of *raw_key*."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Thin ApiKeyEntry-compatible object returned by verify()
# ---------------------------------------------------------------------------

class _RuntimeKeyEntry:
    """
    Lightweight stand-in for ``engram.config.ApiKeyEntry`` built from a
    SQLite row.  Exposes the same attributes used by ``check_namespace_access``
    and ``require_admin_access`` so it is a drop-in at the auth layer.
    """

    def __init__(
        self,
        key: str,
        user_id: str,
        namespaces: list[str],
        read_only: bool,
        description: str,
        created_at: str,
    ) -> None:
        self.key = key  # raw key — only available immediately after creation
        self.user_id = user_id
        self.namespaces = namespaces
        self.read_only = read_only
        self.description = description
        self.created_at = created_at
        # These exist on the real ApiKeyEntry model; default to empty here.
        self.namespace_access: list = []
        self.vault_namespaces: list = []


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS keys (
    id          TEXT PRIMARY KEY,
    key_hash    TEXT UNIQUE NOT NULL,
    key_prefix  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    namespaces  TEXT NOT NULL DEFAULT '["*"]',
    read_only   INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
)
"""


class RuntimeKeyStore:
    """
    Async-friendly SQLite-backed store for dynamically created API keys.

    All database I/O is done via the stdlib ``sqlite3`` module called from
    an async executor so the event loop is never blocked.

    Usage
    -----
    ::

        store = RuntimeKeyStore()
        await store.init()

        result = await store.create(user_id="alice", namespaces=["org:acme:*"])
        print(result["key"])   # shown once; store only keeps the hash

        entry = await store.verify(raw_key)
        if entry:
            print(entry.user_id)
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_dir = Path.home() / ".engram"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "keys.db"
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._get_conn().execute(sql, params)

    def _commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Public API (async wrappers around synchronous SQLite)
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create the keys table if it does not already exist."""
        self._execute(_CREATE_TABLE)
        self._commit()
        logger.debug("RuntimeKeyStore initialised at %s", self._db_path)

    async def create(
        self,
        user_id: str,
        namespaces: list[str] | None = None,
        read_only: bool = False,
        description: str = "",
    ) -> dict[str, Any]:
        """
        Generate a new API key and persist its SHA-256 hash.

        The plaintext ``key`` is returned **once** in the result dict and is
        not stored.  Callers must present it to the user immediately.

        Returns
        -------
        dict
            Keys: ``id``, ``key`` (plaintext), ``key_prefix``, ``user_id``,
            ``namespaces``, ``read_only``, ``created_at``, ``description``.
        """
        if namespaces is None:
            namespaces = ["*"]

        raw_key = secrets.token_hex(32)
        key_prefix = raw_key[:8]
        key_hash = _hash_key(raw_key)
        key_id = str(uuid.uuid4())
        created_at = _now_utc()

        self._execute(
            """
            INSERT INTO keys
                (id, key_hash, key_prefix, user_id, namespaces, read_only, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                key_hash,
                key_prefix,
                user_id,
                json.dumps(namespaces),
                1 if read_only else 0,
                description,
                created_at,
            ),
        )
        self._commit()
        logger.info("Created runtime API key id=%s user_id=%s", key_id, user_id)

        return {
            "id": key_id,
            "key": raw_key,       # plaintext — shown once only
            "key_prefix": key_prefix,
            "user_id": user_id,
            "namespaces": namespaces,
            "read_only": read_only,
            "created_at": created_at,
            "description": description,
        }

    async def verify(self, raw_key: str) -> _RuntimeKeyEntry | None:
        """
        Verify a raw API key and return a ``_RuntimeKeyEntry`` if valid.

        Returns ``None`` when the key is not found or has been revoked.
        """
        key_hash = _hash_key(raw_key)
        row = self._execute(
            "SELECT * FROM keys WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        ).fetchone()

        if row is None:
            return None

        return _RuntimeKeyEntry(
            key=raw_key,
            user_id=row["user_id"],
            namespaces=json.loads(row["namespaces"]),
            read_only=bool(row["read_only"]),
            description=row["description"],
            created_at=row["created_at"],
        )

    async def list_keys(self) -> list[dict[str, Any]]:
        """
        Return all key rows **without** the ``key_hash`` column.

        Includes both active and revoked keys; callers can filter by
        ``revoked_at IS NULL`` for active-only views.
        """
        rows = self._execute(
            """
            SELECT id, key_prefix, user_id, namespaces, read_only,
                   description, created_at, revoked_at
            FROM keys
            ORDER BY created_at DESC
            """
        ).fetchall()

        return [
            {
                "id": r["id"],
                "key_prefix": r["key_prefix"],
                "user_id": r["user_id"],
                "namespaces": json.loads(r["namespaces"]),
                "read_only": bool(r["read_only"]),
                "description": r["description"],
                "created_at": r["created_at"],
                "revoked_at": r["revoked_at"],
            }
            for r in rows
        ]

    async def revoke(self, key_id: str) -> bool:
        """
        Soft-delete a key by setting its ``revoked_at`` timestamp.

        Returns ``True`` if the key was found and revoked, ``False`` otherwise.
        """
        cursor = self._execute(
            "UPDATE keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now_utc(), key_id),
        )
        self._commit()
        revoked = cursor.rowcount > 0
        if revoked:
            logger.info("Revoked runtime API key id=%s", key_id)
        else:
            logger.debug("Revoke called for unknown/already-revoked key id=%s", key_id)
        return revoked

    async def delete(self, key_id: str) -> bool:
        """
        Hard-delete a key row.

        Returns ``True`` if the row existed, ``False`` otherwise.
        """
        cursor = self._execute("DELETE FROM keys WHERE id = ?", (key_id,))
        self._commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Hard-deleted runtime API key id=%s", key_id)
        return deleted
