"""memnos background CONSOLIDATION scheduler — the "sleep pass".

Stored memories aren't useful until they're distilled: raw facts → entity dossiers
that pre-join multi-hop and close out superseded beliefs. This runs offline (LLM) on a
schedule (LaunchAgent) so live recall quality holds up without paying an LLM at query
time.

DIRTY-only by design: a namespace is consolidated only if it has facts NEWER than its
newest dossier (or has facts but no dossier yet). Idle namespaces are skipped, so the
nightly run is cheap and idempotent. Per-namespace work + cost is recorded to
memnos_control.usage_ledger so `memnos_admin.py usage` shows the consolidation spend.

Runs direct-DB (like the canary) — needs the LLM, not the HTTP surface.
Usage: OPENAI_API_KEY=... python memnos_consolidate.py [--all] [--ns NAMESPACE]
"""
import argparse
import os
import sys

sys.path.insert(0, ".")


def _load_env(path=".env"):
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()

import psycopg
from psycopg.rows import dict_row
from openai import OpenAI

from core import BrainStore
from core.service import MemnosMemory
from core.control import Control
from validate_brain import CachedEmbedder
from locomo_pg_parallel import TSCostMeter

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_core@localhost:5433/memnos")
SCHEMA = "tenant_memnos"


def dirty_namespaces(conn):
    """Namespaces with facts newer than their newest dossier (or with facts but no
    dossier). These are the only ones worth re-consolidating."""
    with conn.cursor() as c:
        c.execute(f"""
            SELECT s.namespace,
                   max(s.created_at) FILTER (WHERE s.kind='fact')    AS last_fact,
                   max(s.created_at) FILTER (WHERE s.kind='dossier') AS last_dossier,
                   count(*) FILTER (WHERE s.kind='fact')             AS n_facts
            FROM {SCHEMA}.semantic s
            GROUP BY s.namespace
            HAVING count(*) FILTER (WHERE s.kind='fact') > 0
        """)
        rows = c.fetchall()
    out = []
    for r in rows:
        if r["last_dossier"] is None or (r["last_fact"] and r["last_fact"] > r["last_dossier"]):
            out.append((r["namespace"], r["n_facts"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="consolidate every namespace (ignore dirty check)")
    ap.add_argument("--ns", help="consolidate a single namespace")
    ap.add_argument("--max-entities", type=int, default=30)
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY required (consolidation needs the LLM)")

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter()
    embed = CachedEmbedder(cli, meter)
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    store = BrainStore(conn=conn)

    # schema may not exist yet (fresh server) — nothing to do
    with conn.cursor() as c:
        c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (SCHEMA,))
        if not c.fetchone()["exists"]:
            print("[consolidate] no tenant_memnos schema yet — nothing to do")
            return

    if args.ns:
        targets = [(args.ns, None)]
    elif args.all:
        with conn.cursor() as c:
            c.execute(f"SELECT DISTINCT namespace FROM {SCHEMA}.semantic WHERE kind='fact'")
            targets = [(r["namespace"], None) for r in c.fetchall()]
    else:
        targets = dirty_namespaces(conn)

    if not targets:
        print("[consolidate] no dirty namespaces — all dossiers fresh")
        return

    # on_usage feeds dossier-LLM tokens to the meter so the per-namespace cost recorded
    # to usage_ledger reflects real spend (not just embeddings).
    mem = MemnosMemory(store, embed, dim=embed.dim, llm=cli,
                       on_usage=lambda model, pt, ct: meter.record("consolidate", model, pt, ct))
    total = 0
    for ns, n_facts in targets:
        cost0 = meter.cost
        try:
            out = mem.consolidate(ns, max_entities=args.max_entities)
        except Exception as e:
            print(f"[consolidate] {ns}: FAILED {type(e).__name__}: {e}")
            continue
        spent = round(meter.cost - cost0, 6)
        Control.record_usage(conn, None, ns, "consolidate", "gpt-4o-mini", 0, 0, spent)
        total += out.get("dossiers", 0)
        print(f"[consolidate] {ns}: {out.get('dossiers', 0)} dossiers (${spent})")
    print(f"[consolidate] done — {total} dossiers across {len(targets)} namespace(s) ({meter.summary()})")


if __name__ == "__main__":
    main()
