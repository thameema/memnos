"""memnos-migrate — run pending ArcadeDB migrations from the command line."""
import asyncio
import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Run pending memnos ArcadeDB migrations")
    parser.add_argument("--config", default="memnos.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show pending migrations without applying")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    asyncio.run(_run(args))


async def _run(args):
    from memnos.config import MemnosConfig
    from memnos.storage.arcadedb_client import ArcadeDBClient
    from memnos.migrations.runner import run_pending, _get_applied_versions, _load_migrations, _ensure_schema_version_type

    config = MemnosConfig.from_yaml(args.config)
    arcade = ArcadeDBClient(
        host=config.arcadedb.host,
        port=config.arcadedb.port,
        username=config.arcadedb.username,
        password=config.arcadedb.password,
        database=config.arcadedb.database,
    )
    await arcade.init()

    if args.dry_run:
        await _ensure_schema_version_type(arcade)
        applied = await _get_applied_versions(arcade)
        all_migrations = _load_migrations()
        pending = [m for m in all_migrations if m.version not in applied]
        if pending:
            print(f"Pending migrations ({len(pending)}):")
            for m in pending:
                print(f"  {m.version:03d}: {m.description}")
        else:
            print("No pending migrations.")
        await arcade.close()
        return

    applied = await run_pending(arcade)
    await arcade.close()
    if applied:
        print(f"Applied {len(applied)} migration(s): {applied}")
    else:
        print("Already up to date.")
