"""No-AI regression tests for the phased-connection discipline (P0: never hold a pool
connection across slow non-DB work — LLM calls, network embeddings, heavy CPU).

Covers the contracts that changed:
  - recall() == recall_rank(recall_fetch(...))  (DB/CPU split must not change ranking)
  - recall_wide() == recall_wide_rank(recall_wide_fetch(...))
  - remember_turn(vec=...) precomputed-embedding path stores the same turn
  - MemnosMemory(store_or_dsn=None) construction (server phased dispatcher)
  - consolidate(conn_factory=...) read/write phases on short-lived conns (llm=None: 0
    dossiers, no error, self.store never touched)
  - segment_episodes(conn_factory=...) parity with the plain-store path
  - Control.deliver_pending(conn_factory=...) webhook delivery without a held conn

Direct-DB engine tests (no server, no LLM, no OpenAI): a deterministic fake embedder
stands in for the network embedder. Run: MEMNOS_DSN=... python tests/test_perf_phases.py
"""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.service import MemnosMemory
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:perfphases"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def fake_embed(text, _dim=[None]):
    """Deterministic pseudo-embedding (no network). Dim discovered from the schema."""
    h = hashlib.sha256((text or "").encode()).digest()
    dim = _dim[0]
    v = [0.0] * dim
    for i in range(64):
        v[(h[i % 32] * 7 + i) % dim] = ((h[(i * 3) % 32] / 255.0) - 0.5)
    return v


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        eids = [r["id"] for r in c.fetchall()]
        if eids:
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "entities", "provenance", "semantic", "episodic", "raw_turns"):
            try:
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))
            except psycopg.errors.UndefinedColumn:
                conn.rollback()
        c.execute("DELETE FROM memnos_control.subscriptions WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    with conn.cursor() as c:
        c.execute(f"SELECT atttypmod FROM pg_attribute WHERE attrelid='{SCHEMA}.raw_turns'::regclass "
                  f"AND attname='embedding'")
        dim = c.fetchone()["atttypmod"]
    fake_embed.__defaults__[0][0] = dim
    cleanup(conn)
    store = BrainStore(conn=conn)
    mem = MemnosMemory(store, fake_embed, dim=dim, llm=None)

    def conn_factory():
        return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)

    print("== store_or_dsn=None construction (server phased dispatcher) ==")
    mnone = MemnosMemory(None, fake_embed, dim=dim, llm=None)
    check("store is None, schema resolved", mnone.store is None and mnone.schema == SCHEMA)
    facts = mnone.extract_facts("anything", datetime.now(timezone.utc))
    check("extract_facts works with no store (llm=None -> [])", facts == [])

    print("== remember_turn precomputed-vec path ==")
    t0 = datetime(2024, 5, 1, tzinfo=timezone.utc)
    vec = fake_embed("Ada moved to Lisbon in 2024.")
    tid1, rt1, _ = mem.remember_turn(NS, "Ada moved to Lisbon in 2024.", session_id="s1",
                                     observed_at=t0, vec=vec)
    tid2, rt2, _ = mem.remember_turn(NS, "Ada moved to Lisbon in 2024. (default path)",
                                     session_id="s1", observed_at=t0 + timedelta(minutes=1))
    with conn.cursor() as c:
        c.execute(f"SELECT id, text, embedding IS NOT NULL AS has_vec FROM {SCHEMA}.raw_turns "
                  f"WHERE namespace=%s ORDER BY id", (NS,))
        rows = c.fetchall()
    check("both turns stored with embeddings", len(rows) == 2 and all(r["has_vec"] for r in rows))
    check("redacted text returned", rt1 == "Ada moved to Lisbon in 2024.")

    print("== recall fetch/rank split parity ==")
    # seed facts (one entity-rich, one dated) through the normal write path
    mem.write_facts(NS, [
        {"subject": "Ada", "predicate": "lives_in", "object": "Lisbon",
         "statement": "Ada lives in Lisbon as of May 2024."},
        {"subject": "Ada", "predicate": "did_activity", "object": "surfing",
         "statement": "Ada tried surfing in April 2024."},
        {"subject": "Bob", "predicate": "works_at", "object": "Initech",
         "statement": "Bob works at Initech."},
    ], t0, tid1)
    for q in ("Where does Ada live?", "When did Ada try surfing?", "what happened in April 2024"):
        whole = mem.recall(NS, q)
        split = mem.recall_rank(q, mem.recall_fetch(NS, q))
        check(f"recall split parity: {q!r}", whole == split)
    whole = mem.recall_wide([NS], "Where does Ada live?")
    raw_c, sem_c = mem.recall_wide_fetch([NS], "Where does Ada live?")
    split = mem.recall_wide_rank("Where does Ada live?", raw_c, sem_c)
    check("recall_wide split parity", whole == split)
    check("recall_wide_fetch empty namespaces", mem.recall_wide_fetch([], "q") == ([], []))

    print("== consolidate(conn_factory=...) ==")
    out = mnone.consolidate(NS, conn_factory=conn_factory)   # llm=None: read+write phases only
    check("consolidate with conn_factory + no store (llm=None)", out == {"dossiers": 0})
    out2 = mem.consolidate(NS)                               # default plain-store path intact
    check("consolidate default path intact", out2 == {"dossiers": 0})

    print("== segment_episodes(conn_factory=...) parity ==")
    # two sessions -> two episodes; run via conn_factory with NO store on the engine
    out = mnone.segment_episodes(NS, conn_factory=conn_factory)
    check("segments via conn_factory", out == {"episodes": 1})   # both turns same session s1
    # new turns in another session -> incremental segmentation via the DEFAULT path
    mem.remember_turn(NS, "Later note in another session.", session_id="s2",
                      observed_at=t0 + timedelta(hours=2))
    out = mem.segment_episodes(NS)
    check("default path still segments incrementally", out == {"episodes": 1})
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.episodic WHERE namespace=%s", (NS,))
        check("episodes persisted", c.fetchone()["n"] == 2)

    print("== deliver_pending(conn_factory=...) ==")
    pid = Control.create_principal(conn, "perfphases-bot", "agent")
    sub = Control.subscribe(conn, pid, NS, webhook="https://example.invalid/hook")
    mem.remember_turn(NS, "post-subscribe memory", session_id="s3",
                      observed_at=t0 + timedelta(hours=3))
    delivered = []

    def fake_post(url, payload):
        delivered.append((url, len(payload["events"])))

    res = Control.deliver_pending(None, fake_post, conn_factory=conn_factory)
    mine = [r for r in res if r["subscription_id"] == sub["subscription_id"]]
    check("webhook delivered via short-lived conns", mine and mine[0].get("delivered") == 1)
    check("post_fn called once with 1 event", delivered == [("https://example.invalid/hook", 1)])
    res2 = Control.deliver_pending(conn, fake_post)          # legacy held-conn path intact
    check("legacy conn path: nothing pending", not [r for r in res2
                                                    if r["subscription_id"] == sub["subscription_id"]])

    Control.unsubscribe(conn, pid, sub["subscription_id"])
    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
