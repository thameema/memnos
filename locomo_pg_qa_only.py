"""LoCoMo TUNING driver — QA-only, reuses already-ingested tenant_locomo* schemas
(no DROP, no re-ingest). Lets us sweep the high-leverage retrieval/answer/judge
knobs for cents instead of re-paying the ~$0.50 ingest each time.

Tunable vs the bare baseline (locomo_pg_parallel.py defaults):
  --k          candidate pool from hybrid search      (baseline 20)
  --top-k      memories kept after rerank → context   (baseline 8)
  --max-chars  context block size fed to the answerer (baseline 2000)
  --answer-model / --judge-model  (baseline both gpt-4o-mini)

Query embedding stays OpenAI text-embedding-3-small (1536-d) to match what the
schemas were ingested with — do NOT change the embedder here or vectors mismatch.

Run only AFTER the ingest run has fully populated the schemas.

Usage:
    OPENAI_API_KEY=... python locomo_pg_qa_only.py --sample-ids all \
        --k 30 --top-k 12 --max-chars 5000 --judge-model gpt-4o --workers 10 --budget 3
"""
import argparse
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_core import local_models
from memnos_core import retrieve as ret
from memnos_core.embedders import OpenAIEmbedder
from memnos_core.storage import PgStorage
from memnos_core.usage import BudgetExceeded
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP

DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def schema_ready(st, schema):
    with st.conn.cursor() as c:
        c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
        if not c.fetchone()["exists"]:
            return 0
        c.execute(f"SELECT count(*) AS n FROM {schema}.memory")
        return c.fetchone()["n"]


STRICT_SYS = ("Answer ONLY from the retrieved memories. "
              "Be concise (one short phrase). If absent, say 'Not mentioned.'")
RELAXED_SYS = ("Use the retrieved memories as your primary evidence and reason over them "
               "to answer. It is fine to make a small, well-supported inference from the "
               "memories. Give your best concise answer (a short phrase). Only say "
               "'Not mentioned' if there is truly no relevant information.")
COT_SYS = ("You answer questions from retrieved conversation memories. Some answers require "
           "connecting facts across MULTIPLE memories (multi-hop) or reasoning about dates/order "
           "(temporal). Work step by step: (1) note which memories are relevant, (2) connect them / "
           "do any date arithmetic, (3) conclude. Then output the final answer on its own last line "
           "as 'ANSWER: <concise answer>'. If truly absent, use 'ANSWER: Not mentioned.'")


def answer_from_ctx(cli, model, q, ctx, meter, relaxed=False, cot=False):
    sys = COT_SYS if cot else (RELAXED_SYS if relaxed else STRICT_SYS)
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=400 if cot else 120,
        messages=[{"role": "system", "content": sys},
                  {"role": "user", "content": f"Memories:\n{ctx}\n\nQuestion: {q}\nAnswer:"}])
    meter.record("answer", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    txt = r.choices[0].message.content.strip()
    if cot and "ANSWER:" in txt:
        txt = txt.rsplit("ANSWER:", 1)[1].strip()      # extract final answer from the reasoning
    return txt


def judge(cli, model, q, expected, predicted, meter):
    # Slightly more lenient, semantics-based grading prompt (fairer cross-vendor match).
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=4,
        messages=[{"role": "user", "content": f"Question: {q}\nReference answer: {expected}\n"
                   f"Model answer: {predicted}\nDoes the model answer convey the same key information "
                   "as the reference (ignore wording/format)? Reply YES or NO."}])
    meter.record("judge", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return 1 if r.choices[0].message.content.strip().upper().startswith("YES") else 0


def expand_queries(cli, model, question, n, meter):
    """mnemory-style: generate up to n short sub-queries covering different angles,
    so single-embedding recall misses (multi-hop / paraphrase) get covered."""
    import json
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=200,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content":
                   f"Generate up to {n} short, diverse search queries (different angles, "
                   "entities, and sub-questions) that would help retrieve information to "
                   'answer the user question. Return JSON {"queries": ["...", ...]}.'},
                  {"role": "user", "content": question}])
    meter.record("expand", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    try:
        qs = json.loads(r.choices[0].message.content).get("queries", [])
        return [str(x) for x in qs if str(x).strip()][:n]
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


def reorder_first_last(items):
    """Beat 'lost-in-the-middle': place the highest-ranked items at the START and
    END of the context, lower-ranked in the middle. items must be rank-desc."""
    front, back = [], []
    for i, it in enumerate(items):
        (front if i % 2 == 0 else back).append(it)
    return front + back[::-1]


def fetch_neighbors(stg, schema, ns, ids):
    """±1 adjacent turns (memory id is insertion order = turn order) for coherence."""
    if not ids:
        return []
    with stg.conn.cursor() as c:
        c.execute(f"SELECT id, content FROM {schema}.memory WHERE namespace=%s AND id = ANY(%s)",
                  (ns, list(ids)))
        return c.fetchall()


def qa_parallel(schema, ns, qa, cli, ans_model, judge_model, meter, embed_fn,
                workers, rerank_lock, k, top_k, max_chars, relaxed=False, multi_query=0,
                phase1=False, tau=0.5, min_keep=5, max_keep=20, neighbors=False, cot=False):
    pool = Queue()
    for _ in range(workers):
        pool.put(PgStorage(DSN))

    def do_qa(q):
        question, expected = q.get("question", ""), str(q.get("answer", ""))
        cat = CATEGORY_MAP.get(q.get("category"), "unknown")
        if not question or not expected:
            return None
        queries = [question]
        if multi_query > 0:
            queries += expand_queries(cli, "gpt-4o-mini", question, multi_query, meter)
        stg = pool.get()
        try:
            merged = {}
            for qq in queries:
                qv = embed_fn(qq)
                for c in stg.hybrid_search(schema, ns, qq, qv, k=k, top_k=k):
                    merged.setdefault(c["id"], c)
            cands = list(merged.values())

            # rerank the FULL fused pool, keeping scores
            if len(cands) > 1:
                with rerank_lock:
                    order = local_models.rerank(question, [c["content"] for c in cands])
            else:
                order = [(0, 1.0)] if cands else []

            if phase1:
                # adaptive truncation: keep reranked cands whose sigmoid(score)>=tau,
                # clamped to [min_keep, max_keep] — tight, high-signal context.
                kept = []
                for idx, score in order:
                    p = 1.0 / (1.0 + math.exp(-score))
                    if len(kept) < min_keep or (p >= tau and len(kept) < max_keep):
                        kept.append(cands[idx])
                    if len(kept) >= max_keep:
                        break
                if neighbors and kept:
                    kept_ids = {c["id"] for c in kept}
                    want = set()
                    for c in kept:
                        want.add(c["id"] - 1); want.add(c["id"] + 1)
                    want -= kept_ids
                    kept = kept + fetch_neighbors(stg, schema, ns, want)
                final = reorder_first_last(kept)        # best-first-and-last
            else:
                final = [cands[i] for i, _ in order][:top_k]
        finally:
            pool.put(stg)
        ctx = ret.context_block(final, max_chars=max_chars)
        pred = answer_from_ctx(cli, ans_model, question, ctx, meter, relaxed=relaxed, cot=cot)
        return cat, judge(cli, judge_model, question, expected, pred, meter)

    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(do_qa, qa):
            if r:
                out.append(r)
    while not pool.empty():
        pool.get().conn.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="all")
    ap.add_argument("--k", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--max-chars", type=int, default=5000)
    ap.add_argument("--answer-model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--budget", type=float, default=3.00)
    ap.add_argument("--relaxed", action="store_true", help="relaxed answering prompt (allow small inference)")
    ap.add_argument("--multi-query", type=int, default=0, help="N>0: expand into N sub-queries, merge, rerank")
    ap.add_argument("--phase1", action="store_true", help="adaptive truncation + best-first-last reorder")
    ap.add_argument("--tau", type=float, default=0.5, help="phase1 rerank sigmoid keep threshold")
    ap.add_argument("--min-keep", type=int, default=5)
    ap.add_argument("--max-keep", type=int, default=20)
    ap.add_argument("--neighbors", action="store_true", help="phase1: include +-1 neighbor turns")
    ap.add_argument("--cot", action="store_true", help="chain-of-thought answer (reason then ANSWER:)")
    args = ap.parse_args()

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter(budget_usd=args.budget)
    embedder = OpenAIEmbedder(cli, meter)   # must match ingest embedder (1536-d)
    embed_fn = embedder.embed
    rerank_lock = threading.Lock()
    local_models.rerank("warm", ["a", "b"])

    data = load_dataset()
    ids = list(range(len(data))) if args.sample_ids == "all" else [int(x) for x in args.sample_ids.split(",")]

    print(f"CONFIG: k={args.k} top_k={args.top_k} max_chars={args.max_chars} "
          f"answer={args.answer_model} judge={args.judge_model} relaxed={args.relaxed} "
          f"multi_query={args.multi_query} phase1={args.phase1} tau={args.tau} "
          f"keep=[{args.min_keep},{args.max_keep}] neighbors={args.neighbors} cot={args.cot}", flush=True)
    results = []
    st = PgStorage(DSN)
    try:
        for idx in ids:
            sample = data[idx]
            sid = str(sample.get("sample_id", idx))
            schema = f"tenant_locomo{idx}"
            ns = f"locomo:{sid}"
            n = schema_ready(st, schema)
            if not n:
                print(f"[{idx}] SKIP — schema {schema} empty/missing (ingest not done)", flush=True)
                continue
            qa = sample.get("qa", [])
            t0 = time.perf_counter()
            res = qa_parallel(schema, ns, qa, cli, args.answer_model, args.judge_model,
                              meter, embed_fn, args.workers, rerank_lock,
                              args.k, args.top_k, args.max_chars, relaxed=args.relaxed,
                              multi_query=args.multi_query, phase1=args.phase1, tau=args.tau,
                              min_keep=args.min_keep, max_keep=args.max_keep, neighbors=args.neighbors,
                              cot=args.cot)
            results.extend(res)
            print(f"[{idx}] {schema} ({n} mem) — {len(res)} QA in {time.perf_counter()-t0:.0f}s "
                  f"({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== LoCoMo TUNED (QA-only) ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
