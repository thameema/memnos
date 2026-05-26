"""
tools/migrate_affects_edges.py — Backfill AFFECTS graph edges for existing decisions.

Run once to create Entity nodes and AFFECTS edges for any decision/constraint/adr
memory whose affects[] list is non-empty but lacks graph edges.

Usage:
    PYTHONPATH=packages/core python3 tools/migrate_affects_edges.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "packages/core"))

from engram.storage.arcadedb_client import ArcadeDBClient
from engram.models import Entity

ARCADEDB_HOST = os.environ.get("ARCADEDB_HOST", "localhost")
ARCADEDB_PORT = int(os.environ.get("ARCADEDB_PORT", 2480))
ARCADEDB_PASSWORD = os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password")


async def main():
    client = ArcadeDBClient(
        host=ARCADEDB_HOST,
        port=ARCADEDB_PORT,
        password=ARCADEDB_PASSWORD,
        database="engram",
    )
    await client.init()

    # Find all decisions/constraints/adrs with non-empty affects
    rows = await client._query(
        "SELECT id, namespace, affects FROM Memory "
        "WHERE memory_type IN ['decision', 'constraint', 'adr'] "
        "AND affects IS NOT NULL "
        "AND status = 'active' "
        "LIMIT 2000",
        {},
    )

    total = created = skipped = 0
    for row in rows:
        memory_id = row.get("id")
        namespace = row.get("namespace", "")
        affects = row.get("affects") or []
        for entity_name in affects:
            if not entity_name or not entity_name.strip():
                continue
            total += 1
            entity_name_lower = entity_name.lower().strip()
            # Check if AFFECTS edge already exists
            existing = await client._query(
                "MATCH {type: Memory, as: m, where: (id = :mid)}"
                "-AFFECTS->"
                "{type: Entity, as: e, where: (name = :ename)} "
                "RETURN e.name as name",
                {"mid": memory_id, "ename": entity_name_lower},
            )
            if existing:
                skipped += 1
                continue
            # Upsert Entity and create AFFECTS edge
            entity = Entity(name=entity_name_lower, entity_type="DECISION", namespace=namespace)
            await client.upsert_entity(entity)
            await client.create_affects_edge(memory_id, entity_name, namespace)
            created += 1
            print(f"  Created: {memory_id[:8]}... -[AFFECTS]-> {entity_name_lower} ({namespace})")

    print(f"\nBackfill complete: {created} edges created, {skipped} already existed, {total} total")


if __name__ == "__main__":
    asyncio.run(main())
