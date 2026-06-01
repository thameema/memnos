"""
memnos.cli.vault_cli — memnos-vault CLI entry point.

Usage
-----
    memnos-vault rotate-kek --namespace org:my-team
    memnos-vault rotate-kek --namespace org:my-team --dry-run
    memnos-vault rotate-kek --namespace "*"

Environment variables
---------------------
    MEMNOS_VAULT_KEY          Current (old) KEK — required
    MEMNOS_VAULT_KEY_NEW      New KEK — required
    MEMNOS_ARCADEDB_HOST      ArcadeDB host (default: localhost)
    MEMNOS_ARCADEDB_PORT      ArcadeDB port (default: 2480)
    MEMNOS_ARCADEDB_DB        ArcadeDB database (default: memnos)
    MEMNOS_ARCADEDB_USER      ArcadeDB user (default: root)
    MEMNOS_ARCADEDB_PASS      ArcadeDB password (default: memnos)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_NAMESPACE = os.environ.get("MEMNOS_NAMESPACE", "org:default")


async def _run_rotate(args: argparse.Namespace) -> int:
    from memnos.config import MemnosConfig
    from memnos.storage.arcadedb_client import ArcadeDBClient
    from memnos.vault.vault_client import rotate_kek_local

    old_kek = args.old_key or os.environ.get("MEMNOS_VAULT_KEY", "")
    new_kek = args.new_key or os.environ.get("MEMNOS_VAULT_KEY_NEW", "")

    if not old_kek:
        logger.error("Old KEK not provided. Set --old-key or MEMNOS_VAULT_KEY.")
        return 1
    if not new_kek:
        logger.error("New KEK not provided. Set --new-key or MEMNOS_VAULT_KEY_NEW.")
        return 1
    if old_kek == new_kek:
        logger.error("Old and new KEK are identical — nothing to rotate.")
        return 1

    config = MemnosConfig.from_yaml()
    db = ArcadeDBClient(config.arcadedb)
    try:
        await db.start()
        result = await rotate_kek_local(
            old_kek_str=old_kek,
            new_kek_str=new_kek,
            arcadedb_client=db,
            namespace=args.namespace,
            dry_run=args.dry_run,
        )
    finally:
        await db.close()

    mode = "[DRY RUN] " if args.dry_run else ""
    logger.info("%sKEK rotation complete.", mode)
    logger.info("  Rotated : %d", result.rotated)
    if args.dry_run:
        logger.info("  Skipped (dry-run): %d", result.skipped)
    if result.failed:
        logger.error("  Failed  : %d", len(result.failed))
        for sid, err in result.failed:
            logger.error("    %s — %s", sid, err)
        return 2
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memnos-vault",
        description="memnos vault management commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rotate = sub.add_parser(
        "rotate-kek",
        help="Re-encrypt all secret DEKs under a new KEK (local mode only)",
    )
    rotate.add_argument(
        "--namespace", "-n",
        default=_DEFAULT_NAMESPACE,
        help="Namespace scope (default: %(default)s). Use '*' for all namespaces.",
    )
    rotate.add_argument(
        "--old-key",
        default="",
        help="Current KEK (base64 or passphrase). Defaults to MEMNOS_VAULT_KEY env var.",
    )
    rotate.add_argument(
        "--new-key",
        default="",
        help="Replacement KEK. Defaults to MEMNOS_VAULT_KEY_NEW env var.",
    )
    rotate.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate decryption with old KEK but skip DB writes.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "rotate-kek":
        rc = asyncio.run(_run_rotate(args))
        sys.exit(rc)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
