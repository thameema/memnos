"""DATA on the product-critical temporal capability: 'never serve stale as current'.

Not the LoCoMo date-arithmetic category — the thing that actually earns trust. After
ingest, the engine has detected belief-changes (same subject+predicate, value changed
across sessions) and set valid_to on the old value. For EACH real belief-change found
in the LoCoMo conversations we measure:
  - current_served : does current-aware recall return the CURRENT value?
  - stale_suppressed: does current-aware recall EXCLUDE the stale value? (the trust guarantee)
  - stale_leak_naive: would naive recall (no supersession) have leaked the stale value?
Pure embeddings (free). This is evidence, not a hand-crafted demo."""
import sys
sys.path.insert(0, ".")
from openai import OpenAI
from memnos_brain import BrainStore
from validate_brain import CachedEmbedder
from locomo_pg_parallel import TSCostMeter


def norm(s):
    return " ".join(str(s).lower().split())


def main():
    cli = OpenAI(max_retries=5)
    embed = CachedEmbedder(cli, TSCostMeter())
    st = BrainStore("postgresql://memnos:memnos_core@localhost:5433/memnos")

    pairs = []
    for idx in (2, 3, 4):
        schema = f"tenant_pa{idx}"
        sid = None
        with st.conn.cursor() as c:
            c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
            if not c.fetchone()["exists"]:
                continue
            c.execute(f"SELECT DISTINCT namespace FROM {schema}.semantic LIMIT 1")
            row = c.fetchone(); ns = row["namespace"] if row else None
            # belief-change pairs: same (subject, predicate) with both a current and a stale value
            c.execute(f"""
                SELECT subject_entity, predicate,
                  (array_agg(object) FILTER (WHERE valid_to IS NULL))[1] AS cur,
                  (array_agg(object) FILTER (WHERE valid_to IS NOT NULL))[1] AS stale
                FROM {schema}.semantic
                WHERE kind='fact' AND subject_entity IS NOT NULL AND predicate IS NOT NULL
                  AND object IS NOT NULL AND expired_at IS NULL
                GROUP BY subject_entity, predicate
                HAVING count(*) FILTER (WHERE valid_to IS NULL) >= 1
                   AND count(*) FILTER (WHERE valid_to IS NOT NULL) >= 1""")
            for r in c.fetchall():
                if r["cur"] and r["stale"] and norm(r["cur"]) != norm(r["stale"]):
                    pairs.append((schema, ns, r["subject_entity"], r["predicate"], r["cur"], r["stale"]))

    n = len(pairs)
    print(f"\n=== Belief-changes detected in REAL LoCoMo conversations: {n} ===")
    if not n:
        print("(none — supersession did not fire; the trust guarantee is unverified)")
        return
    served = suppressed = leak = 0
    for schema, ns, subj, pred, cur, stale in pairs[:200]:
        q = f"what is {subj}'s {pred.replace('_', ' ')}"
        qv = embed(q)
        cur_rows = st.search_semantic_temporal(schema, ns, qv, q, 15, current_only=True)
        naive_rows = st.search_semantic(schema, ns, qv, q, 15)
        cur_txt = " || ".join(norm(r["content"]) for r in cur_rows[:6])
        naive_txt = " || ".join(norm(r["content"]) for r in naive_rows[:6])
        if norm(cur) in cur_txt:
            served += 1
        if norm(stale) not in cur_txt:
            suppressed += 1
        if norm(stale) in naive_txt:
            leak += 1

    m = min(n, 200)
    print(f"  current value SERVED (current-aware recall):   {served}/{m} = {100*served/m:.0f}%")
    print(f"  stale value SUPPRESSED (trust guarantee):      {suppressed}/{m} = {100*suppressed/m:.0f}%")
    print(f"  stale value LEAKED by NAIVE recall (no supersn): {leak}/{m} = {100*leak/m:.0f}%")
    print("\nREAD: high 'served' + high 'suppressed' = current-vs-stale works on real data.")
    print("      'leaked by naive' = the failures supersession PREVENTS.")
    print(f"\nquery-embed cost: {embed.meter.summary() if hasattr(embed,'meter') else 'n/a'}")


if __name__ == "__main__":
    main()
