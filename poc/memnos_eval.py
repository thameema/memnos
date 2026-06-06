"""memnos quality canary — the 'is it still working accurately?' signal for the pilot.

Measures stale-suppression (never serve a superseded fact as current) + current-served
on real belief-change pairs, and RECORDS the result to memnos_control.eval_runs so the
trend is tracked over time. Run on a schedule (LaunchAgent) so a regression in recall
quality surfaces in `memnos_admin.py quality` / `health` before users feel it.

Default target = benchmark schemas (tenant_pa2,3,4, 1536-d) as a stable regression
canary. Pure embeddings (free). Usage: OPENAI_API_KEY=... python memnos_eval.py
"""
import os
import sys

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from openai import OpenAI
from memnos_brain import BrainStore
from memnos_brain.control import Control
from validate_brain import CachedEmbedder
from locomo_pg_parallel import TSCostMeter

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
SCHEMAS = os.environ.get("MEMNOS_EVAL_SCHEMAS", "tenant_pa2,tenant_pa3,tenant_pa4").split(",")


def norm(s):
    return " ".join(str(s).lower().split())


def main():
    cli = OpenAI(max_retries=5)
    embed = CachedEmbedder(cli, TSCostMeter())
    st = BrainStore(DSN)
    pairs = []
    for schema in SCHEMAS:
        with st.conn.cursor() as c:
            c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
            if not c.fetchone()["exists"]:
                continue
            c.execute(f"SELECT DISTINCT namespace FROM {schema}.semantic LIMIT 1")
            r = c.fetchone()
            if not r:
                continue
            ns = r["namespace"]
            c.execute(f"""SELECT subject_entity, predicate,
                  (array_agg(object) FILTER (WHERE valid_to IS NULL))[1] cur,
                  (array_agg(object) FILTER (WHERE valid_to IS NOT NULL))[1] stale
                FROM {schema}.semantic WHERE kind='fact' AND subject_entity IS NOT NULL
                  AND predicate IS NOT NULL AND object IS NOT NULL AND expired_at IS NULL
                GROUP BY subject_entity, predicate
                HAVING count(*) FILTER (WHERE valid_to IS NULL)>=1
                   AND count(*) FILTER (WHERE valid_to IS NOT NULL)>=1""")
            for row in c.fetchall():
                if row["cur"] and row["stale"] and norm(row["cur"]) != norm(row["stale"]):
                    pairs.append((schema, ns, row["subject_entity"], row["predicate"], row["cur"], row["stale"]))

    n = len(pairs)
    served = suppressed = leak = 0
    for schema, ns, subj, pred, cur, stale in pairs:
        q = f"what is {subj}'s {pred.replace('_', ' ')}"
        qv = embed(q)
        cur_txt = " || ".join(norm(r["content"]) for r in
                              st.search_semantic_temporal(schema, ns, qv, q, 15, current_only=True)[:6])
        naive_txt = " || ".join(norm(r["content"]) for r in st.search_semantic(schema, ns, qv, q, 15)[:6])
        served += norm(cur) in cur_txt
        suppressed += norm(stale) not in cur_txt
        leak += norm(stale) in naive_txt

    supp_rate = (suppressed / n) if n else None
    served_rate = (served / n) if n else None
    leak_rate = (leak / n) if n else None
    with st.conn.cursor() as c:
        conn = st.conn
        if n:
            Control.record_eval(conn, "stale_suppression", "rate", round(supp_rate, 4), n,
                                {"served": round(served_rate, 4), "naive_leak": round(leak_rate, 4)})
            Control.record_eval(conn, "current_served", "rate", round(served_rate, 4), n)
    print(f"[eval] belief-changes n={n}  stale_suppressed={supp_rate}  current_served={served_rate}  "
          f"naive_leak={leak_rate}  -> recorded to eval_runs")


if __name__ == "__main__":
    main()
