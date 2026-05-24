"""
engram.cli.community_cli — engram-community CLI entry point.

Usage
-----
    engram-community detect --namespace org:my-team
    engram-community detect --namespace "*"
    engram-community detect --namespace org:my-team --min-size 3
    engram-community list --namespace org:my-team
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def _detect(args: argparse.Namespace) -> int:
    from engram.config import EngramConfig
    from engram.storage.arcadedb_client import ArcadeDBClient
    from engram.community.detector import detect_communities

    try:
        cfg = EngramConfig.from_yaml(args.config) if args.config else EngramConfig.load()
    except Exception:
        cfg = None

    arcade_cfg = getattr(cfg, "arcadedb", None) if cfg else None
    client = ArcadeDBClient(config=arcade_cfg)

    try:
        await client.init()
    except Exception as exc:
        print(f"ERROR: could not connect to ArcadeDB: {exc}", file=sys.stderr)
        return 1

    try:
        results = await detect_communities(
            arcadedb_client=client,
            namespace=args.namespace,
            min_size=args.min_size,
            persist=True,
        )
    except Exception as exc:
        print(f"ERROR: community detection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()

    if not results:
        print(f"No communities found for namespace: {args.namespace}")
        print("Tip: communities require at least 2 entities co-appearing in the same memory.")
        return 0

    print(f"Detected {len(results)} communities in namespace: {args.namespace}")
    print()
    for r in results:
        print(f"  [{r.community_id}] {r.label}  ({r.member_count} members)")
    return 0


async def _list(args: argparse.Namespace) -> int:
    from engram.config import EngramConfig
    from engram.storage.arcadedb_client import ArcadeDBClient

    try:
        cfg = EngramConfig.from_yaml(args.config) if args.config else EngramConfig.load()
    except Exception:
        cfg = None

    arcade_cfg = getattr(cfg, "arcadedb", None) if cfg else None
    client = ArcadeDBClient(config=arcade_cfg)

    try:
        await client.init()
    except Exception as exc:
        print(f"ERROR: could not connect to ArcadeDB: {exc}", file=sys.stderr)
        return 1

    try:
        communities = await client.list_communities(args.namespace)
    except Exception as exc:
        print(f"ERROR: could not list communities: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()

    if not communities:
        print(f"No communities found for namespace: {args.namespace}")
        print("Run 'engram-community detect' to build communities.")
        return 0

    print(f"Communities in namespace: {args.namespace}")
    print()
    for c in communities:
        print(
            f"  [{c['id']}] {c['label']}  ({c['member_count']} members)  "
            f"detected: {c['detected_at']}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engram-community",
        description="engram community detection — find entity clusters in the knowledge graph.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to engram YAML config file (default: auto-detect from $ENGRAM_CONFIG)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # detect subcommand
    detect_parser = subparsers.add_parser(
        "detect",
        help="Run community detection and persist results to ArcadeDB",
    )
    detect_parser.add_argument(
        "--namespace",
        required=True,
        help="Namespace to run detection on (use '*' for all namespaces)",
    )
    detect_parser.add_argument(
        "--min-size",
        type=int,
        default=2,
        dest="min_size",
        help="Minimum community size (default: 2)",
    )

    # list subcommand
    list_parser = subparsers.add_parser(
        "list",
        help="List detected communities for a namespace",
    )
    list_parser.add_argument(
        "--namespace",
        required=True,
        help="Namespace to list communities for",
    )

    args = parser.parse_args()

    if args.command == "detect":
        sys.exit(asyncio.run(_detect(args)))
    elif args.command == "list":
        sys.exit(asyncio.run(_list(args)))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
