"""
engram.cli.decay_cli — engram-decay CLI entry point.

Usage
-----
    engram-decay --namespace org:my-team
    engram-decay --namespace org:my-team --dry-run
    engram-decay --namespace org:my-team --max-age-days 180 --max-idle-days 60
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_SERVER    = os.environ.get("ENGRAM_SERVER_URL", "http://localhost:8766")
_DEFAULT_NAMESPACE = os.environ.get("ENGRAM_NAMESPACE", "org:default")


async def _run(args: argparse.Namespace) -> int:
    from engram.config import EngramConfig
    from engram.storage.arcadedb_client import ArcadeDBClient
    from engram.decay.job import run_decay_job, _DEFAULT_MAX_AGE_DAYS, _DEFAULT_MAX_IDLE_DAYS

    try:
        cfg = EngramConfig.load()
    except Exception:
        cfg = None

    client = ArcadeDBClient(config=cfg.arcadedb if cfg else None)
    try:
        await client.init_schema()
    except Exception as exc:
        print(f"ERROR: could not connect to ArcadeDB: {exc}", file=sys.stderr)
        return 1

    max_age  = args.max_age_days  if args.max_age_days  is not None else _DEFAULT_MAX_AGE_DAYS
    max_idle = args.max_idle_days if args.max_idle_days is not None else _DEFAULT_MAX_IDLE_DAYS

    report = await run_decay_job(
        client,
        args.namespace,
        dry_run=args.dry_run,
        max_age_days=max_age,
        max_idle_days=max_idle,
    )

    if report.dry_run:
        print(f"[DRY RUN] namespace: {report.namespace}")
    else:
        print(f"namespace: {report.namespace}")

    print(f"  time_weighted expired  ({max_age}d threshold): {len(report.time_weighted_deprecated)} memories")
    print(f"  access_weighted idle   ({max_idle}d threshold): {len(report.access_weighted_deprecated)} memories")
    print(f"  total deprecated: {report.total_deprecated}")

    if report.errors:
        for e in report.errors:
            print(f"  WARNING: {e}", file=sys.stderr)

    if report.dry_run and report.total_deprecated > 0:
        print("\nRe-run without --dry-run to apply changes.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engram-decay",
        description="Run the memory decay job — mark stale memories as deprecated.",
    )
    parser.add_argument(
        "--namespace", default=_DEFAULT_NAMESPACE,
        help="Namespace to scan (default: $ENGRAM_NAMESPACE or 'org:default')",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report candidates without writing any changes",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=None,
        help="time_weighted: deprecate memories older than N days (default: 365)",
    )
    parser.add_argument(
        "--max-idle-days", type=int, default=None,
        help="access_weighted: deprecate memories idle for N days (default: 90)",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
