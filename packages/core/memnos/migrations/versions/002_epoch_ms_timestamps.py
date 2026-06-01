"""Migration 002 — Convert created_at and superseded_at from ISO strings to epoch ms integers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

version = 2
description = "Convert created_at and superseded_at from ISO strings to epoch ms integers"

_SAMPLE_LIMIT = 50


async def up(arcade) -> None:
    """Verify timestamp fields are numeric; convert any ISO string values found.

    ArcadeDB stores DATETIME as epoch ms internally, but early versions of the
    Python code inserted ISO strings via the STRING type. This migration scans
    a sample and corrects any remaining string-typed timestamps.
    """
    rows = await arcade.execute(
        "SELECT @rid, id, created_at, superseded_at FROM Memory LIMIT :lim",
        {"lim": _SAMPLE_LIMIT},
    )

    to_convert: list[dict] = []
    for row in rows:
        needs_update = False
        updates: dict = {}
        for field in ("created_at", "superseded_at"):
            val = row.get(field)
            if isinstance(val, str) and val:
                try:
                    dt = datetime.fromisoformat(val.replace(" ", "T").replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    updates[field] = int(dt.timestamp() * 1000)
                    needs_update = True
                except ValueError:
                    logger.warning("Migration 002: unparseable %s=%r on record %s", field, val, row.get("id", "?"))
        if needs_update:
            updates["rid"] = row["@rid"]
            to_convert.append(updates)

    if not to_convert:
        logger.info("Migration 002: all timestamps already numeric, no action needed")
        return

    logger.info("Migration 002: found %d record(s) with string timestamps to convert", len(to_convert))
    for rec in to_convert:
        rid = rec["rid"]
        set_parts = []
        params: dict = {"rid": rid}
        for field in ("created_at", "superseded_at"):
            if field in rec:
                set_parts.append(f"{field} = :{field}")
                params[field] = rec[field]
        if set_parts:
            await arcade.execute_command(
                f"UPDATE Memory SET {', '.join(set_parts)} WHERE @rid = :rid",
                params,
            )
    logger.info("Migration 002: converted %d record(s)", len(to_convert))
