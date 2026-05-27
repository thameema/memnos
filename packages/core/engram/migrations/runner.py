"""Migration runner — runs pending ArcadeDB schema/data migrations at startup."""

from __future__ import annotations

import importlib
import logging
import time
from types import ModuleType

logger = logging.getLogger(__name__)

# Ordered list of migration module paths. Add new migrations here.
_MIGRATION_MODULES = [
    "engram.migrations.versions.001_initial_schema",
    "engram.migrations.versions.002_epoch_ms_timestamps",
    "engram.migrations.versions.003_affects_edges_backfill",
]


async def run_pending(arcade) -> list[int]:
    """Ensure SchemaVersion type exists, then run all un-applied migrations in order.

    Returns list of version numbers applied this run (empty if already up to date).
    """
    await _ensure_schema_version_type(arcade)
    applied = await _get_applied_versions(arcade)
    migrations = _load_migrations()

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        max_version = max(applied) if applied else 0
        logger.info("Schema at version %d, no migrations pending", max_version)
        return []

    applied_this_run: list[int] = []
    for migration in pending:
        logger.info("Migration %03d (%s): starting", migration.version, migration.description)
        t0 = time.monotonic()
        await migration.up(arcade)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await _record_version(arcade, migration.version, migration.description)
        logger.info("Migration %03d: applied in %dms", migration.version, elapsed_ms)
        applied_this_run.append(migration.version)

    return applied_this_run


async def _ensure_schema_version_type(arcade) -> None:
    cmds = [
        "CREATE VERTEX TYPE SchemaVersion IF NOT EXISTS",
        "CREATE PROPERTY SchemaVersion.version IF NOT EXISTS INTEGER",
        "CREATE PROPERTY SchemaVersion.name IF NOT EXISTS STRING",
        "CREATE PROPERTY SchemaVersion.applied_at IF NOT EXISTS LONG",
        "CREATE INDEX IF NOT EXISTS ON SchemaVersion (version) UNIQUE",
    ]
    for cmd in cmds:
        try:
            await arcade.execute_command(cmd)
        except Exception as exc:
            logger.debug("SchemaVersion init cmd skipped: %s | %s", cmd[:60], exc)


async def _get_applied_versions(arcade) -> set[int]:
    try:
        rows = await arcade.execute("SELECT version FROM SchemaVersion")
        return {int(r["version"]) for r in rows if r.get("version") is not None}
    except Exception:
        return set()


async def _record_version(arcade, version: int, name: str) -> None:
    from engram.time import now_ms
    await arcade.execute_command(
        "INSERT INTO SchemaVersion SET version = :v, name = :n, applied_at = :ts",
        {"v": version, "n": name, "ts": now_ms()},
    )


def _load_migrations() -> list[ModuleType]:
    migrations: list[ModuleType] = []
    for module_path in _MIGRATION_MODULES:
        try:
            mod = importlib.import_module(module_path)
            if not hasattr(mod, "version") or not hasattr(mod, "description") or not hasattr(mod, "up"):
                logger.warning("Migration module %r missing required attributes (version, description, up) — skipping", module_path)
                continue
            migrations.append(mod)
        except ImportError as exc:
            logger.warning("Could not import migration module %r: %s", module_path, exc)
    migrations.sort(key=lambda m: m.version)
    return migrations
