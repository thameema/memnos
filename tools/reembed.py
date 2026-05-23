#!/usr/bin/env python3.10
"""
reembed.py — Re-embed all Memory records in ArcadeDB with the current OpenAI embedder.

Fixes the dimension mismatch that occurs when switching from local (384-dim)
to OpenAI text-embedding-3-small (1536-dim). After re-embedding, drops and
recreates the HNSW vector index with the correct dimensions.

Usage:
    python3.10 tools/reembed.py --batch-size 50 --dry-run
    python3.10 tools/reembed.py --batch-size 50
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
    from openai import OpenAI
except ImportError as e:
    print(f"[error] Missing package: {e}\nRun: pip install httpx openai", file=sys.stderr)
    sys.exit(1)

ARCADEDB_URL = "http://localhost:2480"
DB_NAME = "engram"
VECTOR_DIM = 1536  # OpenAI text-embedding-3-small
EMBED_MODEL = "text-embedding-3-small"


def _auth_header() -> dict:
    password = os.environ.get("ARCADEDB_PASSWORD", "engram-dev-password")
    creds = base64.b64encode(f"root:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def arcade_query(sql: str, params: dict | None = None) -> list[dict]:
    body = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/query/{DB_NAME}",
        content=json.dumps(body),
        headers=_auth_header(),
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def arcade_command(sql: str, params: dict | None = None) -> list[dict]:
    body = {"language": "sql", "command": sql}
    if params:
        body["params"] = params
    resp = httpx.post(
        f"{ARCADEDB_URL}/api/v1/command/{DB_NAME}",
        content=json.dumps(body),
        headers=_auth_header(),
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def get_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        # Try loading from .env in engram repo
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        print("[error] OPENAI_API_KEY not set. Export it or put it in .env", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_memories_batch(skip: int, limit: int) -> list[dict]:
    """Fetch a batch of Memory records (id + content + current embedding dim)."""
    rows = arcade_query(
        "SELECT id, content FROM Memory ORDER BY created_at ASC SKIP :skip LIMIT :limit",
        {"skip": skip, "limit": limit},
    )
    return rows


def count_memories() -> int:
    rows = arcade_query("SELECT count(*) AS cnt FROM Memory")
    return int(rows[0].get("cnt", 0)) if rows else 0


_MAX_CHARS = 20000  # ~5000 tokens, well within OpenAI 8192-token limit
_TRUNC_CHARS = 6000  # fallback if first attempt fails


def _truncate(text: str, limit: int) -> str:
    return text[:limit] if len(text) > limit else text


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with OpenAI, returns list of float vectors.

    If the batch fails due to token limit, falls back to individual embedding
    with progressive truncation.
    """
    truncated = [_truncate(t, _MAX_CHARS) for t in texts]
    try:
        response = client.embeddings.create(model=EMBED_MODEL, input=truncated)
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
    except Exception:
        # Batch failed — embed each item individually with more aggressive truncation
        results = []
        for t in truncated:
            for limit in (_TRUNC_CHARS, 3000, 1000):
                try:
                    resp = client.embeddings.create(model=EMBED_MODEL, input=[_truncate(t, limit)])
                    results.append(resp.data[0].embedding)
                    break
                except Exception:
                    continue
            else:
                results.append([])  # placeholder for failed item
        return results


def update_embeddings(memory_ids: list[str], embeddings: list[list[float]], dry_run: bool) -> int:
    """Update content_embedding for each memory. Returns count updated."""
    updated = 0
    for mem_id, embedding in zip(memory_ids, embeddings):
        if not embedding:
            continue  # Skip items where embedding failed
        if dry_run:
            updated += 1
            continue
        vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"
        arcade_command(
            f"UPDATE Memory SET content_embedding = {vec_literal} WHERE id = :id",
            {"id": mem_id},
        )
        updated += 1
    return updated


def count_by_dim(target_dim: int) -> tuple[int, int]:
    """Return (count_with_target_dim, count_with_other_dim)."""
    rows = arcade_query("SELECT content_embedding FROM Memory WHERE content_embedding IS NOT NULL LIMIT 5")
    if not rows:
        return 0, 0
    # Use a sample to detect dimension mix
    sample_row = rows[0]
    emb = sample_row.get("content_embedding")
    sample_dim = len(emb) if isinstance(emb, list) else 0
    # Count all memories with wrong dimension
    wrong_rows = arcade_query(
        "SELECT count(*) AS cnt FROM Memory WHERE content_embedding IS NOT NULL",
    )
    total_with_emb = int(wrong_rows[0].get("cnt", 0)) if wrong_rows else 0
    # We can't efficiently count by dimension in SQL, so return totals
    return total_with_emb, 0


def drop_hnsw_index(dry_run: bool) -> None:
    print("Dropping HNSW index on Memory.content_embedding (if exists)...")
    if not dry_run:
        try:
            arcade_command("DROP INDEX Memory[content_embedding] IF EXISTS")
            print("  → Dropped.")
        except Exception as e:
            print(f"  → Drop skipped (may not exist): {e}")
    else:
        print("  [dry-run] Would drop index.")


def create_hnsw_index(dry_run: bool) -> None:
    print(f"Creating HNSW index on Memory.content_embedding (dim={VECTOR_DIM})...")
    sql = (
        f'CREATE INDEX ON Memory (content_embedding) HNSW '
        f'{{"vectorDimensions": {VECTOR_DIM}, "vectorSimilarityFunction": "COSINE"}}'
    )
    if not dry_run:
        try:
            arcade_command(sql)
            print("  → Created.")
        except Exception as e:
            print(f"  [warn] Index creation error: {e}")
    else:
        print("  [dry-run] Would create index.")


def main():
    parser = argparse.ArgumentParser(description="Re-embed all Memory records with OpenAI")
    parser.add_argument("--batch-size", type=int, default=50, help="Records per batch")
    parser.add_argument("--dry-run", action="store_true", help="No writes — just measure")
    parser.add_argument("--skip-index", action="store_true", help="Skip index drop/recreate")
    args = parser.parse_args()

    openai_key = get_openai_key()
    client = OpenAI(api_key=openai_key)

    total = count_memories()
    print(f"Total memories to re-embed: {total}")
    print(f"Batch size: {args.batch_size}")
    print(f"Dry run: {args.dry_run}")
    print()

    if args.dry_run:
        print("[dry-run] No changes will be written.")
        print(f"Would re-embed {total} memories at ~$0.02 per million tokens.")
        # Estimate: average memory ~200 tokens
        est_tokens = total * 200
        est_cost = est_tokens / 1_000_000 * 0.02
        print(f"Estimated tokens: ~{est_tokens:,}")
        print(f"Estimated cost: ~${est_cost:.4f}")
        return

    skip = 0
    ok_count = 0
    fail_count = 0
    start = time.time()

    while skip < total:
        batch = fetch_memories_batch(skip, args.batch_size)
        if not batch:
            break

        mem_ids = [r["id"] for r in batch]
        texts = [r.get("content", "") or "" for r in batch]

        # Filter out empty content
        valid = [(mid, txt) for mid, txt in zip(mem_ids, texts) if txt.strip()]
        if not valid:
            skip += len(batch)
            continue

        valid_ids, valid_texts = zip(*valid)

        try:
            embeddings = embed_batch(client, list(valid_texts))
            updated = update_embeddings(list(valid_ids), embeddings, args.dry_run)
            ok_count += updated
        except Exception as e:
            print(f"  [fail] Batch at skip={skip}: {e}")
            fail_count += len(valid_ids)
            time.sleep(2)

        skip += len(batch)
        elapsed = time.time() - start
        pct = 100 * skip / total
        print(f"  Progress: {skip}/{total} ({pct:.0f}%) — {ok_count} ok, {fail_count} failed ({elapsed:.1f}s)")

        # Rate limit: OpenAI allows 3000 RPM on text-embedding-3-small
        # Batches of 50 = ~1.2 req/s, well within limits — no sleep needed

    print()
    print(f"Re-embedding complete: {ok_count} ok, {fail_count} failed")

    if not args.skip_index and not args.dry_run:
        print()
        drop_hnsw_index(args.dry_run)
        create_hnsw_index(args.dry_run)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
