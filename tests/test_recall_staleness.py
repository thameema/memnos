"""Recall staleness through RAW TURNS (issue #10 residual B).

Fact supersession closes SEMANTIC facts, but /recall's top hits can be kind:'turn'
rows — verbatim old statements with no supersession concept — so answers could still
lead with yesterday's state. Turns are history and must STAY; the fix is conservative
RANKING + ANNOTATION:

  1. a retrieved turn whose derived semantic facts are ALL superseded is annotated
     (superseded: true, superseded_at: <close date>) and demoted below current facts;
  2. current (non-superseded) facts rank above stale-annotated turns;
  3. render_context shows the transition: '- (said, superseded as of <date>) ...'
     next to the current fact (the flagship-demo behavior);
  4. turns with NO derived facts, or with at least one still-live fact, are untouched;
  5. the staleness lookup is ONE batched query over the retrieved turn ids only
     (store.turn_supersession) — never O(namespace).

Engine-level (no LLM, no embedding API): rows are seeded directly with a crafted-vector
stub embedder; recall() runs the REAL production retrieval + rerank + render pipeline.
"""
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:stale"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def main():
    global PASS, FAIL
    store = BrainStore(DSN)
    store.create_schema("memnos")          # additive boot migration (GIN index)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    _auto = {}

    def crafted_embed(text):
        theta = _auto.setdefault(text, 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    def reset():
        with store.conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    reset()
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    d1 = datetime(2026, 6, 8, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 11, tzinfo=timezone.utc)

    # --- seed: the supersession-retest shape -------------------------------------------
    t_stale = "The zeta deployment is currently blocked by a database capacity problem."
    t_fresh = "The zeta deployment is no longer blocked; the capacity problem was resolved."
    t_nofacts = "The zeta deployment standup happens at nine in the morning."
    t_partial = "The zeta deployment runs in us-east and is owned by the platform team."
    cur_fact = "The zeta deployment is unblocked as of 2026-06-11."

    tid_stale = store.insert_raw_turn(SCHEMA, NS, None, "user", t_stale, d1, crafted_embed(t_stale))
    tid_fresh = store.insert_raw_turn(SCHEMA, NS, None, "user", t_fresh, d2, crafted_embed(t_fresh))
    tid_nof = store.insert_raw_turn(SCHEMA, NS, None, "user", t_nofacts, d1, crafted_embed(t_nofacts))
    tid_part = store.insert_raw_turn(SCHEMA, NS, None, "user", t_partial, d1, crafted_embed(t_partial))

    fid_new = store.insert_semantic(SCHEMA, NS, "fact", cur_fact, subject="zeta deployment",
                                    predicate="status", obj="unblocked", valid_from=d2,
                                    vec=crafted_embed(cur_fact), source_turn_ids=[tid_fresh],
                                    observed_at=d2)
    old_fact = "The zeta deployment is blocked by a database capacity problem."
    fid_old = store.insert_semantic(SCHEMA, NS, "fact", old_fact, subject="zeta deployment",
                                    predicate="status", obj="blocked", valid_from=d1,
                                    vec=crafted_embed(old_fact), source_turn_ids=[tid_stale],
                                    observed_at=d1)
    store.close_out(SCHEMA, NS, fid_old, valid_to=d2, superseded_by=fid_new)
    # partial turn: one superseded + one LIVE derived fact -> must stay untouched
    fp1 = store.insert_semantic(SCHEMA, NS, "fact", "The zeta deployment ran in us-west.",
                                subject="zeta deployment", predicate="runs_on", obj="us-west",
                                valid_from=d1, vec=crafted_embed("zeta ran us-west"),
                                source_turn_ids=[tid_part], observed_at=d1)
    store.close_out(SCHEMA, NS, fp1, valid_to=d2, superseded_by=fid_new)
    store.insert_semantic(SCHEMA, NS, "fact", "The zeta deployment is owned by the platform team.",
                          subject="zeta deployment", predicate="owned_by", obj="platform team",
                          valid_from=d1, vec=crafted_embed("zeta owned platform"),
                          source_turn_ids=[tid_part], observed_at=d1)

    # --- 1. batched store lookup (the DB-phase primitive) ------------------------------
    print("=== store.turn_supersession (one batched query) ===")
    sup = store.turn_supersession(SCHEMA, [tid_stale, tid_fresh, tid_nof, tid_part, None])
    check("fully-superseded turn is flagged with its close date",
          tid_stale in sup and sup[tid_stale] is not None
          and sup[tid_stale].astimezone(timezone.utc).date().isoformat() == "2026-06-11")
    check("turn whose facts are live is NOT flagged", tid_fresh not in sup)
    check("turn with NO derived facts is NOT flagged", tid_nof not in sup)
    check("partially-superseded turn is NOT flagged", tid_part not in sup)
    check("empty id list returns {}", store.turn_supersession(SCHEMA, []) == {})

    # --- 2. recall: annotation + demotion ----------------------------------------------
    print("=== recall ranking + annotation ===")
    rows = mem.recall(NS, "is the zeta deployment blocked")
    stale_rows = [r for r in rows if r.get("superseded")]
    check("exactly the stale turn is annotated",
          len(stale_rows) == 1 and stale_rows[0]["content"] == t_stale
          and stale_rows[0]["kind"] == "turn")
    check("annotation carries superseded_at (close date)",
          stale_rows[0].get("superseded_at") == "2026-06-11")
    check("fresh / no-facts / partial turns carry NO annotation",
          all(not r.get("superseded") for r in rows if r["content"] != t_stale))
    idx = {r["content"]: i for i, r in enumerate(rows)}
    fact_idx = [i for i, r in enumerate(rows) if r["kind"] == "fact"]
    check("current fact is retrieved", cur_fact in idx)
    check("stale turn is demoted below EVERY fact row",
          fact_idx and idx[t_stale] > max(fact_idx))
    check("stale turn is demoted below the fresh turn", idx[t_stale] > idx[t_fresh])
    check("stale turn STAYS in the result set (history is kept)", t_stale in idx)

    # --- 3. rendered context: the transition is visible --------------------------------
    print("=== render_context ===")
    ctx = mem.render_context(rows)
    stale_line = next((l for l in ctx.splitlines() if t_stale in l), "")
    check("stale turn line is labeled '(said, superseded as of <date>)'",
          stale_line.startswith("- (said, superseded as of 2026-06-11)"))
    check("current fact is present in the same context (transition visible)",
          cur_fact in ctx)
    check("fresh turn renders as a plain '(said)' line",
          any(l.startswith("- (said)") and t_fresh in l for l in ctx.splitlines()))

    # --- 4. wide recall carries the same annotation -------------------------------------
    print("=== recall_wide ===")
    wrows = mem.recall_wide([NS], "is the zeta deployment blocked")
    wstale = [r for r in wrows if r.get("superseded")]
    check("wide recall annotates the stale turn too",
          len(wstale) == 1 and wstale[0]["content"] == t_stale
          and wstale[0].get("superseded_at") == "2026-06-11")
    widx = {r["content"]: i for i, r in enumerate(wrows)}
    wfacts = [i for i, r in enumerate(wrows) if r["kind"] == "fact"]
    check("wide recall demotes the stale turn below facts",
          wfacts and widx[t_stale] > max(wfacts))

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
