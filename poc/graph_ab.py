"""Graph-traversal A/B: does query-time GRAPH traversal add anything beyond the
offline dossier pre-joining? Ingest ONCE (now populating entities/edges/mentions from
the SPO triples), then run QA TWICE on the same data — graph arm OFF vs ON — and
compare multi_hop. Answers the 'do we need a graph DB?' question with our own data.

Usage: OPENAI_API_KEY=... python graph_ab.py --sample-ids 2,3,4
"""
import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_brain import BrainStore, rerank as brain_rerank
from phaseA_recombine import ingest_sample, retrieve, fmt_context
from locomo_pg_qa_only import answer_from_ctx, judge
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP
from validate_brain import CachedEmbedder

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def score(qa, st_pool, schema, ns, cli, model, judge_model, embed_fn, reranker, rlock, workers, graph):
    def do_qa(q):
        question, expected = q.get("question", ""), str(q.get("answer", ""))
        cat = CATEGORY_MAP.get(q.get("category"), "unknown")
        if not question or not expected:
            return None
        s2 = st_pool.get()
        try:
            rows = retrieve(s2, schema, ns, question, embed_fn, reranker, rlock, k=40, graph=graph)
        finally:
            st_pool.put(s2)
        pred = answer_from_ctx(cli, model, question, fmt_context(rows, 9000), TSCostMeter(), relaxed=True)
        return cat, judge(cli, judge_model, question, expected, pred, TSCostMeter())
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(do_qa, qa):
            if r:
                out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=4.0)
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter(budget_usd=args.budget)
    embed_fn = CachedEmbedder(cli, meter)
    rlock = threading.Lock()
    brain_rerank.rerank("warm", ["a", "b"], "BAAI/bge-reranker-base")
    data = httpx.get(URL, follow_redirects=True, timeout=60).json()
    st = BrainStore(DSN)
    res = {False: [], True: []}

    for idx in ids:
        sample = data[idx]; sid = str(sample.get("sample_id", idx))
        tenant = f"pa{idx}"; schema = f"tenant_{tenant}"; ns = f"locomo:{sid}"
        st.drop_schema(tenant); st.create_schema(tenant, dim=embed_fn.dim)
        t0 = time.perf_counter()
        ing = ingest_sample(st, schema, ns, sample, cli, "gpt-4o-mini", meter, embed_fn, args.workers)
        gc = st.counts(schema)
        print(f"[{idx}] ingested {ing} | graph: {gc['entities']} ents, {gc['edges']} edges, "
              f"{gc['mentions']} mentions ({time.perf_counter()-t0:.0f}s)", flush=True)
        pool = Queue()
        for _ in range(args.workers):
            pool.put(BrainStore(DSN))
        for g in (False, True):
            res[g] += score(sample.get("qa", []), pool, schema, ns, cli, "gpt-4o-mini", "gpt-4o",
                            embed_fn, "BAAI/bge-reranker-base", rlock, args.workers, g)
        while not pool.empty():
            pool.get().conn.close()
        print(f"[{idx}] QA done both arms ({meter.summary()})", flush=True)

    def tab(results):
        cats = {}
        for c, s in results:
            cats.setdefault(c, []).append(s)
        return {c: (sum(v), len(v)) for c, v in cats.items()}, (sum(s for _, s in results), len(results))

    print("\n=== GRAPH-TRAVERSAL A/B (same ingested data) ===")
    for label, g in (("graph OFF", False), ("graph ON ", True)):
        cats, (cor, tot) = tab(res[g])
        line = "  ".join(f"{c}:{v[0]}/{v[1]}={100*v[0]/v[1]:.0f}%" for c, v in sorted(cats.items()))
        print(f"{label}: OVERALL {cor}/{tot}={100*cor/tot:.0f}%  | {line}")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
