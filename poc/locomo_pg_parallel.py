"""LoCoMo driver — PARALLEL edition. Same pipeline/scoring as locomo_pg.py, but
the network-bound OpenAI calls (extract + embed during ingest; embed/answer/judge
during QA) run concurrently across a thread pool, while DB writes stay serial on
one connection (psycopg conns aren't thread-safe). Each QA worker borrows its own
connection from a small pool, so reads parallelize too. Local cross-encoder
rerank is guarded by a lock (sentence-transformers forward pass isn't reentrant).

Wall-clock is extraction-bound; this cuts a ~5h sequential full run to well under
an hour. Cost is identical (same number of tokens) and still hard-capped.

Usage:
    OPENAI_API_KEY=... python locomo_pg_parallel.py --sample-ids all --max-qa 0 \
        --workers 10 --budget 18.00
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

from memnos_poc import ingest as ing
from memnos_poc import local_models
from memnos_poc import retrieve as ret
from memnos_poc.embedders import OpenAIEmbedder, LocalEmbedder
from memnos_poc.storage import PgStorage
from memnos_poc.usage import CostMeter, BudgetExceeded

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "adversarial"}


class TSCostMeter(CostMeter):
    """Thread-safe meter: lock around record() so concurrent calls accumulate
    correctly and the budget cap stays authoritative."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._lock = threading.Lock()

    def record(self, *a, **k):
        with self._lock:
            return super().record(*a, **k)


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def sample_turns(sample, gate_min_chars=12):
    """Flatten a sample's sessions into an ordered [(role, 'spk: text')] list,
    applying the same trivial-turn gate as the sequential pipeline."""
    conv = sample.get("conversation", {})
    sess_keys = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                       key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
    turns = []
    for sk in sess_keys:
        date = str(conv.get(f"{sk}_date_time", "")).strip()   # temporal anchor
        for t in conv[sk] if isinstance(conv.get(sk), list) else []:
            if not isinstance(t, dict):
                continue
            text, spk = t.get("text", ""), t.get("speaker", "?")
            if text and len(text.strip()) >= gate_min_chars:
                prefix = f"[{date}] " if date else ""
                turns.append(("user", f"{prefix}{spk}: {text}"))
    return turns


def ingest_parallel(st, schema, ns, turns, cli, model, meter, do_extract, embed_fn, workers):
    """Phase A (parallel, network-bound): embed + extract every turn concurrently.
    Phase B (serial, on the single conn): write episode/memory/entities/facts in
    original order — so the DB only ever sees one writer."""
    def precompute(turn):
        role, text = turn
        vec = embed_fn(text)
        ents, facts = ([], [])
        if do_extract:
            ents, facts = ing.extract(cli, model, text, meter)
        return role, text, vec, ents, facts

    with ThreadPoolExecutor(max_workers=workers) as ex:
        computed = list(ex.map(precompute, turns))  # map preserves input order

    for role, text, vec, ents, facts in computed:
        ep = st.insert_episode(schema, ns, role, text)
        ent_ids = []
        for e in ents:
            name = str(e.get("name", "")).strip().lower()
            if name:
                ent_ids.append(st.insert_entity(schema, ns, name[:100],
                                                 str(e.get("type", "CONCEPT")).upper()[:32]))
        st.insert_memory(schema, ns, text, vec, entity_ids=ent_ids)
        for f in facts:
            s, p, o = (str(f.get(k, "")).strip().lower() for k in ("subject", "predicate", "object"))
            if s and p and o:
                st.insert_fact(schema, ns, s[:100], p[:60], o[:200], source_episode_id=ep)
    return len(computed)


def answer_from_ctx(cli, model, q, ctx, meter):
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=120,
        messages=[{"role": "system", "content": "Answer ONLY from the retrieved memories. "
                   "Be concise (one short phrase). If absent, say 'Not mentioned.'"},
                  {"role": "user", "content": f"Memories:\n{ctx}\n\nQuestion: {q}\nAnswer:"}])
    meter.record("answer", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return r.choices[0].message.content.strip()


def judge(cli, model, q, expected, predicted, meter):
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=4,
        messages=[{"role": "user", "content": f"Question: {q}\nExpected: {expected}\nPredicted: {predicted}\n"
                   "Is the predicted answer correct? Reply YES or NO."}])
    meter.record("judge", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return 1 if r.choices[0].message.content.strip().upper().startswith("YES") else 0


def qa_parallel(schema, ns, qa, cli, model, meter, embed_fn, workers, rerank_lock):
    """Each QA runs on its own borrowed connection (safe concurrent reads); the
    local rerank is the only non-reentrant bit, so it's lock-guarded."""
    pool = Queue()
    for _ in range(workers):
        pool.put(PgStorage(DSN))

    def do_qa(q):
        question, expected = q.get("question", ""), str(q.get("answer", ""))
        cat = CATEGORY_MAP.get(q.get("category"), "unknown")
        if not question or not expected:
            return None
        stg = pool.get()
        try:
            qvec = embed_fn(question)
            cands = stg.hybrid_search(schema, ns, question, qvec, k=20, top_k=20)
        finally:
            pool.put(stg)
        if len(cands) > 1:
            with rerank_lock:
                order = local_models.rerank(question, [c["content"] for c in cands])
            cands = [cands[i] for i, _ in order]
        ctx = ret.context_block(cands[:8])
        pred = answer_from_ctx(cli, model, question, ctx, meter)
        return cat, judge(cli, model, question, expected, pred, meter)

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
    ap.add_argument("--max-qa", type=int, default=0, help="0 = all QA per sample")
    ap.add_argument("--budget", type=float, default=18.00)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--local-embed", action="store_true")
    ap.add_argument("--ingest-only", action="store_true", help="re-ingest only, skip QA (--model sets extraction model)")
    args = ap.parse_args()

    cli = OpenAI(max_retries=5)            # ride out 429s under concurrency
    meter = TSCostMeter(budget_usd=args.budget)
    embedder = LocalEmbedder() if args.local_embed else OpenAIEmbedder(cli, meter)
    embed_fn = embedder.embed
    rerank_lock = threading.Lock()

    # warm local models once (avoid lazy-load races inside the pool)
    local_models.rerank("warm", ["a", "b"])
    if args.local_embed:
        local_models.embed("warm")

    data = load_dataset()
    ids = list(range(len(data))) if args.sample_ids == "all" else [int(x) for x in args.sample_ids.split(",")]

    results = []
    st = PgStorage(DSN)
    try:
        for idx in ids:
            sample = data[idx]
            sid = str(sample.get("sample_id", idx))
            schema = f"tenant_locomo{idx}"
            ns = f"locomo:{sid}"
            with st.conn.cursor() as c:
                c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                c.execute("SELECT create_tenant_schema(%s, %s)", (f"locomo{idx}", embedder.dim))
            turns = sample_turns(sample)
            t0 = time.perf_counter()
            n = ingest_parallel(st, schema, ns, turns, cli, args.model, meter,
                                not args.no_extract, embed_fn, args.workers)
            print(f"[{idx}] ingested {n} turns in {time.perf_counter()-t0:.0f}s   ({meter.summary()})", flush=True)

            if args.ingest_only:
                continue
            qa = sample.get("qa", [])
            qa = qa[: args.max_qa] if args.max_qa else qa
            t1 = time.perf_counter()
            sample_res = qa_parallel(schema, ns, qa, cli, args.model, meter, embed_fn, args.workers, rerank_lock)
            results.extend(sample_res)
            print(f"[{idx}] answered {len(sample_res)} QA in {time.perf_counter()-t1:.0f}s   ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== LoCoMo (PG engine, parallel) ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
