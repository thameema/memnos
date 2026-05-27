"""Migration 003 — Backfill AFFECTS graph edges for decisions/constraints/ADRs."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

version = 3
description = "Backfill AFFECTS graph edges for decisions/constraints/ADRs that have affects[] but no edge"


async def up(arcade) -> None:
    """Create AFFECTS edges for decision-type memories whose affects list has no corresponding edge.

    Idempotent: checks for edge existence before creating; safe to re-run.
    """
    rows = await arcade.execute(
        "SELECT @rid AS rid, id, namespace, affects FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "AND status = 'active' "
        "AND affects IS NOT NULL "
        "LIMIT 2000",
    )

    created = 0
    already_existed = 0

    for row in rows:
        memory_rid = row.get("rid") or row.get("@rid", "")
        memory_id = row.get("id", "")
        namespace = row.get("namespace", "")
        affects = row.get("affects") or []

        for entity_name in affects:
            if not entity_name or not entity_name.strip():
                continue
            entity_name_lower = entity_name.lower().strip()

            # Check if AFFECTS edge already exists using MATCH traversal
            try:
                existing = await arcade.execute(
                    "MATCH {type: Memory, as: m, where: (id = :mid)}"
                    "-AFFECTS->"
                    "{type: Entity, as: e, where: (name = :ename)} "
                    "RETURN e.name as name",
                    {"mid": memory_id, "ename": entity_name_lower},
                )
                if existing:
                    already_existed += 1
                    continue
            except Exception as exc:
                logger.debug("Migration 003: edge-check failed for %s → %s: %s", memory_id, entity_name_lower, exc)

            # Upsert Entity
            try:
                updated = await arcade.execute_command(
                    "UPDATE Entity SET entity_type = 'DECISION' "
                    "WHERE name = :name AND namespace = :ns",
                    {"name": entity_name_lower, "ns": namespace},
                )
            except Exception:
                updated = None

            # INSERT if upsert matched nothing — we can't read count from execute_command,
            # so attempt insert and swallow duplicate errors
            try:
                import uuid
                await arcade.execute_command(
                    "INSERT INTO Entity SET id = :id, name = :name, entity_type = :etype, "
                    "namespace = :ns, created_at = :ts",
                    {
                        "id": str(uuid.uuid4()),
                        "name": entity_name_lower,
                        "etype": "DECISION",
                        "ns": namespace,
                        "ts": _now_ms(),
                    },
                )
            except Exception:
                pass  # entity already exists — upsert above covered it

            # Create AFFECTS edge
            try:
                await arcade.execute_command(
                    "CREATE EDGE AFFECTS "
                    "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
                    "TO (SELECT FROM Entity WHERE name = :ename AND namespace = :ns)",
                    {"mid": memory_id, "ns": namespace, "ename": entity_name_lower},
                )
                created += 1
            except Exception as exc:
                logger.debug("Migration 003: AFFECTS edge skipped for %s → %s: %s", memory_id, entity_name_lower, exc)

    logger.info(
        "Migration 003: backfilled %d AFFECTS edges, %d already existed",
        created, already_existed,
    )


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
