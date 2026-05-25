#!/usr/bin/env python3
"""
migrate_to_qdrant.py — Backfill all ArcadeDB Memory vectors into Qdrant.

Run this ONCE before enabling ENGRAM_VECTOR_BACKEND=qdrant.
If Qdrant already has a point for a memory ID it is silently skipped (idempotent).

Usage:
    python3 tools/migrate_to_qdrant.py --dry-run
    python3 tools/migrate_to_qdrant.py
    python3 tools/migrate_to_qdrant.py --batch-size 100 --qdrant-url http://localhost:6333

The script reads vectors directly from ArcadeDB so no re-embedding occurs.
If a memory has no stored embedding it is skipped with a warning.

Prerequisites:
    pip install httpx qdrant-client
    Qdrant must be running (docker compose --profile qdrant up -d qdrant)
    ArcadeDB must be running
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("[error] httpx not found. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
except ImportError:
    print(
        "[error] qdrant-client not found. Run: pip install 'qdrant-client>=1.9'",
        file=sys.stderr,
    )
    sys.exit(1)

ARCADEDB_URL = os.environ.get("ARCADEDB_HOST_URL", "http://localhost:2480")
DB_NAME = "engram"
COLLECTION = "engram_memories"
DEFAULT_QDRANT_URL = "http://localhost:6333"


# ── ArcadeDB helpers ──────────────────────────────────────────────────────────

def _arcade_auth() -> dict:
    password = os.environ.get("ARCADEDB_PASSWORD", "")
    if not password:
        # Try reading from .env in the repo root
        for candidate in [
            Path(__file__).parent.parent / ".env",
            Path.home() / ".engram" / ".env",
        ]:
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    if line.startswith("ARCADEDB_PASSWORD="):
                        password = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if password:
                break
    if not password:
        password = "engram"  # default from docker-compose
    creds = base64.b64encode(f"root:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def arcade_query(sql: str, params: dict | None = None) -> list[dict]:
    body: dict = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/query/{DB_NAME}",
        content=json.dumps(body),
        headers=_arcade_auth(),
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def count_memories() -> int:
    rows = arcade_query("SELECT count(*) AS cnt FROM Memory WHERE status = 'active'")
    return int(rows[0].get("cnt", 0)) if rows else 0


def fetch_batch(skip: int, limit: int) -> list[dict]:
    return arcade_query(
        "SELECT id, namespace, memory_type, content_embedding "
        "FROM Memory WHERE status = 'active' "
        "ORDER BY created_at ASC SKIP :skip LIMIT :limit",
        {"skip": skip, "limit": limit},
    )


# ── Qdrant helpers ────────────────────────────────────────────────────────────

def get_qdrant_client(url: str, api_key: str | None) -> QdrantClient:
    kwargs: dict = {"url": url}
    if api_key:
        kwargs["api_key"] = api_key
    return QdrantClient(**kwargs)


def ensure_collection(client: QdrantClient, dim: int) -> None:
    existing = [c.name for c in (client.get_collections().collections or [])]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"  Created Qdrant collection '{COLLECTION}' (dim={dim}, cosine)")
    else:
        print(f"  Collection '{COLLECTION}' already exists")


def existing_ids(client: QdrantClient) -> set[str]:
    """Return the set of point IDs already in Qdrant (handles large collections via scroll)."""
    ids: set[str] = set()
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=COLLECTION,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in result:
            ids.add(str(point.id))
        if next_offset is None:
            break
        offset = next_offset
    return ids


def upsert_batch(
    client: QdrantClient,
    records: list[dict],
    already_indexed: set[str],
    collection_dim: int,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    """
    Returns (upserted, skipped_existing, skipped_no_embedding, skipped_wrong_dim).
    Records whose embedding dimension does not match the collection are skipped.
    """
    points: list[PointStruct] = []
    skipped_existing = 0
    skipped_no_emb = 0
    skipped_wrong_dim = 0

    for rec in records:
        rid = str(rec.get("id", ""))
        if not rid:
            continue
        if rid in already_indexed:
            skipped_existing += 1
            continue
        emb = rec.get("content_embedding")
        if not emb or not isinstance(emb, list):
            skipped_no_emb += 1
            continue
        if len(emb) != collection_dim:
            skipped_wrong_dim += 1
            continue

        points.append(
            PointStruct(
                id=rid,
                vector=emb,
                payload={
                    "namespace": rec.get("namespace", ""),
                    "memory_type": rec.get("memory_type", "fact"),
                    "superseded": False,
                },
            )
        )

    upserted = len(points)
    if points and not dry_run:
        client.upsert(collection_name=COLLECTION, points=points)

    return upserted, skipped_existing, skipped_no_emb, skipped_wrong_dim


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill ArcadeDB memory vectors into Qdrant"
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--qdrant-api-key", default=os.environ.get("QDRANT_API_KEY", ""))
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-upsert all points, even those already in Qdrant",
    )
    args = parser.parse_args()

    print("engram → Qdrant backfill migration")
    print(f"  ArcadeDB : {ARCADEDB_URL}")
    print(f"  Qdrant   : {args.qdrant_url}")
    print(f"  Dry run  : {args.dry_run}")
    print()

    # ── Count memories in ArcadeDB ────────────────────────────────────────────
    total = count_memories()
    if total == 0:
        print("No active memories found in ArcadeDB. Nothing to migrate.")
        return
    print(f"Active memories in ArcadeDB: {total}")

    # ── Connect to Qdrant ─────────────────────────────────────────────────────
    qclient = get_qdrant_client(args.qdrant_url, args.qdrant_api_key or None)

    # Detect the majority embedding dimension from a sample of 20 memories
    sample = arcade_query(
        "SELECT content_embedding FROM Memory "
        "WHERE content_embedding IS NOT NULL AND status = 'active' LIMIT 20"
    )
    if not sample or not sample[0].get("content_embedding"):
        print("[error] No memory with a stored embedding found. Run reembed.py first.")
        sys.exit(1)

    from collections import Counter
    dim_counts: Counter = Counter()
    for row in sample:
        emb = row.get("content_embedding")
        if isinstance(emb, list) and emb:
            dim_counts[len(emb)] += 1

    if not dim_counts:
        print("[error] Could not determine embedding dimension from sample.")
        sys.exit(1)

    dim, majority_count = dim_counts.most_common(1)[0]
    print(f"Detected embedding dimension: {dim} (majority={majority_count}/{len(sample)})")

    minority_dims = {d: c for d, c in dim_counts.items() if d != dim}
    if minority_dims:
        minority_total = sum(minority_dims.values())
        print(
            f"[warn] Mixed embedding dimensions detected — {minority_total} sample memories "
            f"have {list(minority_dims.keys())} dimensions and will be skipped."
        )
        print(
            "       Run tools/reembed.py to re-embed all memories to a single dimension, "
            "then re-run this migration."
        )

    if not args.dry_run:
        ensure_collection(qclient, dim)

    # ── Discover already-indexed IDs ──────────────────────────────────────────
    if not args.dry_run and not args.force:
        print("Scanning Qdrant for already-indexed IDs...")
        already_indexed = existing_ids(qclient)
        print(f"  Already in Qdrant: {len(already_indexed)}")
    else:
        already_indexed = set()

    if not args.dry_run and len(already_indexed) == total:
        print(f"All {total} memories are already in Qdrant. Nothing to do.")
        return

    # ── Migrate in batches ────────────────────────────────────────────────────
    skip = 0
    total_upserted = 0
    total_skipped_existing = 0
    total_skipped_no_emb = 0
    total_skipped_wrong_dim = 0
    start = time.time()

    while skip < total:
        batch = fetch_batch(skip, args.batch_size)
        if not batch:
            break

        ups, sk_ex, sk_emb, sk_dim = upsert_batch(
            qclient, batch, already_indexed, dim, args.dry_run
        )
        total_upserted += ups
        total_skipped_existing += sk_ex
        total_skipped_no_emb += sk_emb
        total_skipped_wrong_dim += sk_dim

        # Update our known-indexed set so we don't re-check Qdrant per batch
        for rec in batch:
            already_indexed.add(str(rec.get("id", "")))

        skip += len(batch)
        elapsed = time.time() - start
        pct = 100 * skip / total
        print(
            f"  {skip}/{total} ({pct:.0f}%) — "
            f"upserted={total_upserted}, "
            f"skipped_existing={total_skipped_existing}, "
            f"skipped_no_emb={total_skipped_no_emb}, "
            f"skipped_dim_mismatch={total_skipped_wrong_dim} "
            f"({elapsed:.1f}s)"
        )

    print()
    print("Migration complete")
    print(f"  Upserted                    : {total_upserted}")
    print(f"  Already in Qdrant (skipped) : {total_skipped_existing}")
    print(f"  No embedding (skipped)      : {total_skipped_no_emb}")
    print(f"  Wrong dimension (skipped)   : {total_skipped_wrong_dim}")
    elapsed = time.time() - start
    print(f"  Elapsed                     : {elapsed:.1f}s")
    print()
    if total_skipped_no_emb:
        print(
            f"[warn] {total_skipped_no_emb} memories have no embedding — "
            "run tools/reembed.py to fix them, then re-run this script."
        )
    if total_skipped_wrong_dim:
        print(
            f"[warn] {total_skipped_wrong_dim} memories have wrong embedding dimension "
            f"(expected {dim}-dim) — run tools/reembed.py to normalize all embeddings "
            "to the current model, then re-run this migration."
        )
    if not args.dry_run:
        print()
        print("Next steps:")
        print("  1. Add ENGRAM_VECTOR_BACKEND=qdrant to your .env")
        print("  2. QDRANT_URL=http://qdrant:6333  (or http://localhost:6333 outside Docker)")
        print("  3. docker compose --profile qdrant up -d  (start/keep Qdrant running)")
        print("  4. docker compose restart engram           (pick up new config)")
        print("  5. Verify: curl 'http://localhost:8766/api/v1/memory/search?q=test&ns=all' -H 'X-API-Key: ...' | python3 -m json.tool")


if __name__ == "__main__":
    main()
