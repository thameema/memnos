"""Migration 004 — Add Episode vertex type and episode_ids property to Memory."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

version = 4
description = "Add Episode vertex type and episode_ids property to Memory"


async def up(arcade) -> None:
    """Create the Episode vertex type and add episode_ids to the Memory type.

    Idempotent: all statements use IF NOT EXISTS, safe to re-run.
    """
    cmds = [
        "CREATE VERTEX TYPE Episode IF NOT EXISTS",
        "CREATE PROPERTY Episode.id IF NOT EXISTS STRING",
        "CREATE PROPERTY Episode.title IF NOT EXISTS STRING",
        "CREATE PROPERTY Episode.namespace IF NOT EXISTS STRING",
        "CREATE PROPERTY Episode.summary IF NOT EXISTS STRING",
        "CREATE PROPERTY Episode.tags IF NOT EXISTS LIST",
        "CREATE PROPERTY Episode.created_at IF NOT EXISTS LONG",
        "CREATE PROPERTY Episode.closed_at IF NOT EXISTS LONG",
        "CREATE INDEX IF NOT EXISTS ON Episode (id) NOTUNIQUE",
        "CREATE INDEX IF NOT EXISTS ON Episode (namespace) NOTUNIQUE",
        "CREATE PROPERTY Memory.episode_ids IF NOT EXISTS LIST",
    ]
    for cmd in cmds:
        try:
            await arcade._command(cmd)
        except Exception as exc:
            logger.debug("Migration 004: skipped %r — %s", cmd[:60], exc)

    logger.info("Migration 004: Episode type and Memory.episode_ids ready")
