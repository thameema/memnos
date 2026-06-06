"""Change 1 — index extracted FACTS as first-class searchable memories.

The pipeline extracts SPO facts into the `fact` table but hybrid_search only
searches `memory` (raw turns). This backfill embeds each fact as "subject
predicate object" and inserts it into `memory` (same namespace), so retrieval
surfaces distilled facts alongside raw turns — what mnemory does. No
re-extraction; embedding-only (cents). Idempotent-ish: tags fact-memories with a
content marker so re-runs can be cleaned first.

Usage: OPENAI_API_KEY=... python backfill_facts.py --sample-ids all
"""
import argparse
import sys
sys.path.insert(0, ".")
from openai import OpenAI
from memnos_poc.storage import PgStorage, _vlit

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"


def batched(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="all")
    ap.add_argument("--model", default="text-embedding-3-small")
    args = ap.parse_args()
    ids = list(range(10)) if args.sample_ids == "all" else [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    st = PgStorage(DSN)
    total_facts = 0
    for idx in ids:
        schema = f"tenant_locomo{idx}"
        with st.conn.cursor() as c:
            c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
            if not c.fetchone()["exists"]:
                print(f"[{idx}] skip — {schema} missing")
                continue
            # clean any prior fact-memories so re-runs don't duplicate
            c.execute(f"DELETE FROM {schema}.memory WHERE content LIKE '[fact] %'")
            c.execute(f"SELECT namespace, subject, predicate, object FROM {schema}.fact")
            rows = c.fetchall()
        if not rows:
            print(f"[{idx}] no facts")
            continue
        texts = [f"[fact] {r['subject']} {r['predicate']} {r['object']}" for r in rows]
        nss = [r["namespace"] for r in rows]
        n = 0
        for chunk_idx in batched(list(range(len(texts))), 256):
            sub = [texts[i] for i in chunk_idx]
            resp = cli.embeddings.create(model=args.model, input=sub)
            vecs = [d.embedding for d in resp.data]
            with st.conn.cursor() as c:
                for j, i in enumerate(chunk_idx):
                    c.execute(
                        f"INSERT INTO {schema}.memory(namespace,content,embedding) "
                        f"VALUES(%s,%s,%s::halfvec)",
                        (nss[i], texts[i], _vlit(vecs[j])))
                    n += 1
        print(f"[{idx}] indexed {n} facts as memories into {schema}")
        total_facts += n
    print(f"\nDONE — {total_facts} fact-memories indexed across {len(ids)} schemas")


if __name__ == "__main__":
    main()
