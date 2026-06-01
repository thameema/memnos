"""Migration 001 — Baseline schema."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

version = 1
description = "Baseline schema — all vertex/edge types created by _init_schema()"


async def up(arcade) -> None:
    """No-op: _init_schema() runs before migrations and owns baseline creation.

    We verify the schema is present by checking that the Memory type exists,
    then return immediately.
    """
    try:
        rows = await arcade.execute("SELECT count(*) AS cnt FROM Memory")
        count = int(rows[0].get("cnt", 0)) if rows else 0
        logger.debug("Migration 001: Memory type verified (%d records present)", count)
    except Exception as exc:
        logger.warning("Migration 001: Memory type check failed (may be a fresh DB): %s", exc)
