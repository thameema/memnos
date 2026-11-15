"""Unit + PG tests for Issue #159: Entity graph quality (literal filter, identity
canonicalization, entity embedding at mint time, cleanup_entity_graph backfill, and
knowledge_health graph metrics).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.service import MemnosMemory
from core.store import BrainStore, vlit

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:entity_graph_quality_159"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def fake_embed(text: str, _dim=[int(os.environ.get("MEMNOS_EMBED_DIM", "1536"))]) -> list[float]:
    import hashlib
    import math
    dim = _dim[0]
    vec = [0.0] * dim
    tokens = [t.lower() for t in (text or "").split() if t] or ["_"]
    for tok in tokens:
        h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16], 16)
        idx = h % dim
        sign = 1.0 if ((h >> 16) & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        eids = [r["id"] for r in c.fetchall()]
        if eids:
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "semantic", "raw_turns", "episodic", "entities"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))


def main():
    print("=== 1. Pure classifier: BrainStore.is_valid_entity_name ===")
    for bad_obj in [
        "true", "false", "none", "unknown", "0", "1", "2", "$25,732", "higher",
        "not showing at all", "at 10:50-10:51 AM", "three smaller, lower-risk pieces",
        "Henniya $76.92, Farid $300, A-to-Z $1", "2026-11-14", "Nov 14, 2026",
        ":8900", "99.9%", "waiting on CI", "because the pool was full",
        "a very long free text clause that has more than five words in it",
    ]:
        check(f"rejects literal/clause object {bad_obj!r}",
              not BrainStore.is_valid_entity_name(bad_obj, is_object=True))

    for good_obj in [
        "PostgreSQL", "MySQL", "Redis", "host3", "PR #138", "AVAX-USD",
        "Thameem Ansari", "memnos_server.py", "OpenAI", "Claude Code",
        "In Memory Cache", "On Call Rotation", "For Loop", "Acme, Inc.",
    ]:
        check(f"accepts named entity/identifier object {good_obj!r}",
              BrainStore.is_valid_entity_name(good_obj, is_object=True))

    print("=== 2. Identity canonicalization: BrainStore.canonicalize_entity_name ===")
    check("user -> Thameem Ansari when user_name='Thameem Ansari'",
          BrainStore.canonicalize_entity_name("user", user_name="Thameem Ansari") == "Thameem Ansari")
    check("User -> Thameem Ansari",
          BrainStore.canonicalize_entity_name("User", user_name="Thameem Ansari") == "Thameem Ansari")
    check("the user -> Thameem Ansari",
          BrainStore.canonicalize_entity_name("the user", user_name="Thameem Ansari") == "Thameem Ansari")
    check("thameem ansari (case-variant) -> Thameem Ansari",
          BrainStore.canonicalize_entity_name("thameem ansari", user_name="Thameem Ansari") == "Thameem Ansari")
    check("first-name-only 'Thameem' is NOT collapsed into full user_name",
          BrainStore.canonicalize_entity_name("Thameem", user_name="Thameem Ansari") == "Thameem")
    check("coding nouns ('model', 'agent', 'owner') are NOT rewritten",
          all(BrainStore.canonicalize_entity_name(w, user_name="Thameem Ansari", is_object=True) == w
              for w in ("model", "agent", "owner")))
    check("user -> 'user' when user_name=None",
          BrainStore.canonicalize_entity_name("User", user_name=None) == "user")
    check("The Assistant -> 'assistant'",
          BrainStore.canonicalize_entity_name("The Assistant", user_name="Thameem Ansari") == "assistant")

    print("=== 3. Write path + Backfill + Health metrics against Postgres ===")
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    with conn.cursor() as c:
        c.execute(f"SELECT atttypmod FROM pg_attribute WHERE attrelid='{SCHEMA}.raw_turns'::regclass AND attname='embedding'")
        row = c.fetchone()
        if row and row["atttypmod"] and row["atttypmod"] > 0:
            fake_embed.__defaults__[0][0] = int(row["atttypmod"])
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    try:
        mem = MemnosMemory(store, fake_embed, dim=fake_embed.__defaults__[0][0], user_name="Thameem Ansari")
        from datetime import datetime, timezone
        now_ts = datetime(2026, 11, 14, tzinfo=timezone.utc)

        # Write facts with mix of named objects and literal objects
        facts = [
            {"subject": "user", "predicate": "uses", "object": "PostgreSQL",
             "statement": "Thameem Ansari uses PostgreSQL."},
            {"subject": "User", "predicate": "prefers", "object": "Claude Code",
             "statement": "Thameem Ansari prefers Claude Code."},
            {"subject": "thameem ansari", "predicate": "spent", "object": "$25,732",
             "statement": "Thameem Ansari spent $25,732."},
            {"subject": "deployment", "predicate": "is_active", "object": "true",
             "statement": "The deployment is active (true)."},
            {"subject": "pipeline", "predicate": "status", "object": "not showing at all",
             "statement": "The pipeline status is not showing at all."},
        ]
        n_f, _ = mem.write_facts(NS, facts, now_ts, turn_id=1, speaker="user")
        check("all 5 semantic facts written", n_f == 5)

        with conn.cursor() as c:
            c.execute(f"SELECT name, (embedding IS NOT NULL) AS has_emb, (embedding <=> %s::{store.vtype}) AS pg_dist FROM {SCHEMA}.entities WHERE namespace=%s ORDER BY name",
                      (str(fake_embed("PostgreSQL")), NS))
            ents = c.fetchall()
        ent_names = [e["name"] for e in ents]
        check("user / User / thameem ansari resolved to single 'Thameem Ansari' entity",
              ent_names.count("Thameem Ansari") == 1 and "user" not in ent_names and "User" not in ent_names,
              str(ent_names))
        check("named objects ('PostgreSQL', 'Claude Code') became entities",
              "PostgreSQL" in ent_names and "Claude Code" in ent_names,
              str(ent_names))
        check("literal/clause objects ('$25,732', 'true', 'not showing at all') were NOT minted as entities",
              all(bad not in ent_names for bad in ("$25,732", "true", "not showing at all")),
              str(ent_names))
        check("all newly minted entities have non-null embeddings",
              all(e["has_emb"] for e in ents),
              str(ents))
        pg_ent = next((e for e in ents if e["name"] == "PostgreSQL"), None)
        check("entity embedding matches entity name vector ('PostgreSQL'), not statement vector",
              pg_ent is not None and float(pg_ent["pg_dist"]) < 1e-5,
              str(pg_ent))

        # Seed legacy junk entities + unembedded alias directly to test cleanup_entity_graph
        with conn.cursor() as c:
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, NULL) RETURNING id",
                      (NS, "true"))
            junk_id = c.fetchone()["id"]
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, NULL) RETURNING id",
                      (NS, "$25,732"))
            junk_id2 = c.fetchone()["id"]
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, NULL) RETURNING id",
                      (NS, "user"))
            alias_id = c.fetchone()["id"]
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, NULL) RETURNING id",
                      (NS, "UnembeddedService"))
            unemb_id = c.fetchone()["id"]
        store.bump_edge(SCHEMA, NS, alias_id, unemb_id, w=2.0)
        store.bump_edge(SCHEMA, NS, junk_id, unemb_id, w=1.0)

        h_before = store.health(SCHEMA, NS)
        check("health() includes new graph quality metrics",
              all(k in h_before for k in ("degree_1_share", "literal_name_share", "embedding_coverage")),
              str(h_before))
        check("literal_name_share > 0 before cleanup",
              (h_before["literal_name_share"] or 0) > 0, str(h_before))
        check("embedding_coverage < 1.0 before cleanup",
              (h_before["embedding_coverage"] or 1.0) < 1.0, str(h_before))

        dry = store.cleanup_entity_graph(SCHEMA, NS, user_name="Thameem Ansari", embed_fn=fake_embed, dry_run=True)
        check("dry_run reports 2 literal entities to drop and 1 alias to merge",
              dry["literal_entities_dropped"] == 2 and dry["alias_entities_merged"] == 1,
              str(dry))

        real = store.cleanup_entity_graph(SCHEMA, NS, user_name="Thameem Ansari", embed_fn=fake_embed, dry_run=False)
        check("real cleanup drops 2 literal entities, merges 1 alias, and preserves all 5 semantic facts",
              real["literal_entities_dropped"] == 2
              and real["alias_entities_merged"] == 1
              and real["facts_before"] == 5
              and real["facts_after"] == 5,
              str(real))

        h_after = store.health(SCHEMA, NS)
        check("literal_name_share == 0.0 after cleanup",
              h_after["literal_name_share"] == 0.0, str(h_after))
        check("embedding_coverage == 1.0 after cleanup",
              h_after["embedding_coverage"] == 1.0, str(h_after))

        # Test embedding donor transfer when an unembedded canonical target merges with an embedded alias
        with conn.cursor() as c:
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, NULL) RETURNING id",
                      (NS, "Redis"))
            redis_unemb_id = c.fetchone()["id"]
            c.execute(f"INSERT INTO {SCHEMA}.entities(namespace, name, embedding) VALUES (%s, %s, %s) RETURNING id",
                      (NS, "redis", vlit(fake_embed("Redis"))))
        donor_stats = store.cleanup_entity_graph(SCHEMA, NS, user_name="Thameem Ansari", dry_run=False)
        check("donor merge merges 1 alias",
              donor_stats["alias_entities_merged"] == 1,
              str(donor_stats))
        with conn.cursor() as c:
            c.execute(f"SELECT (embedding IS NOT NULL) AS has_emb FROM {SCHEMA}.entities WHERE id=%s",
                      (redis_unemb_id,))
            redis_row = c.fetchone()
        check("alias merge preserves non-null embedding from donor onto unembedded canonical target",
              redis_row is not None and bool(redis_row["has_emb"]),
              str(redis_row))
    finally:
        cleanup(conn)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
