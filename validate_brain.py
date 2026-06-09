"""End-to-end validation of the brain-inspired engine (B1+B2+B3) on LoCoMo.

Pipeline per sample: ENCODE (event-segmented episodic, local free embed) ->
CONSOLIDATE (semantic propositions + multi-hop dossiers, gpt-4o-mini) -> retrieve
RECENCY-GATED DUAL (episodic⊕semantic, bge-reranker) -> answer (gpt-4o-mini relaxed)
-> judge (gpt-4o). Scores by category. The one measurement that validates the redesign.

Usage: OPENAI_API_KEY=... python validate_brain.py --sample-ids 2,3,4 --budget 3
"""
import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from queue import Queue

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_brain import BrainStore, Encoder, Consolidator, Retriever, context_block
from memnos_brain import rerank as brain_rerank
from memnos_core import local_models
from locomo_pg_qa_only import answer_from_ctx, judge
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP
from memnos_core.usage import BudgetExceeded

DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

try:
    from dateutil import parser as _dtp
    def parse_date(s, fallback):
        try:
            d = _dtp.parse(s, fuzzy=True)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return fallback
except ImportError:
    def parse_date(s, fallback):
        return fallback


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


class CachedEmbedder:
    """OpenAI text-embedding-3-small (1536-d) with a cache + batch `prime()` so the
    bulk of turns/facts embed in a few calls instead of thousands. Apples-to-apples
    with the raw-turn baseline (same embedding model)."""
    dim = 1536

    def __init__(self, client, meter, model="text-embedding-3-small", dim=1536):
        self.client, self.meter, self.model, self.dim = client, meter, model, dim
        self.cache = {}
        self._lock = threading.Lock()

    def prime(self, texts):
        uniq = [t for t in dict.fromkeys(texts) if t and t not in self.cache]
        for i in range(0, len(uniq), 512):
            chunk = uniq[i:i + 512]
            r = self.client.embeddings.create(model=self.model, input=chunk)
            self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
            for t, d in zip(chunk, r.data):
                self.cache[t] = d.embedding

    def embed(self, text):
        v = self.cache.get(text)
        if v is not None:
            return v
        r = self.client.embeddings.create(model=self.model, input=text)
        self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
        v = r.data[0].embedding
        with self._lock:
            self.cache[text] = v
        return v

    __call__ = embed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--reranker", default="BAAI/bge-reranker-base")
    ap.add_argument("--k", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=14)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=3.0)
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter(budget_usd=args.budget)
    embed_fn = CachedEmbedder(cli, meter)  # OpenAI 1536-d (apples-to-apples w/ baseline)
    rlock = threading.Lock()
    print(f"warming reranker {args.reranker} ...", flush=True)
    brain_rerank.rerank("warm", ["a", "b"], args.reranker)
    data = load_dataset()
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    results = []
    st = BrainStore(DSN)
    try:
        for idx in ids:
            sample = data[idx]; sid = str(sample.get("sample_id", idx))
            tenant = f"brain{idx}"; schema = f"tenant_{tenant}"; ns = f"locomo:{sid}"
            st.drop_schema(tenant); st.create_schema(tenant, dim=embed_fn.dim)

            # --- ENCODE (B1) ---
            conv = sample.get("conversation", {})
            sess = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                          key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
            # batch-prime turn embeddings (fast OpenAI path)
            embed_fn.prime([t["text"] for sk in sess for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                            if isinstance(t, dict) and t.get("text")])
            enc = Encoder(st, schema, ns, embed_fn)
            t0 = time.perf_counter()
            for si, sk in enumerate(sess):
                date = parse_date(str(conv.get(f"{sk}_date_time", "")), base + timedelta(days=30 * si))
                turns = conv[sk] if isinstance(conv.get(sk), list) else []
                for ti, t in enumerate(turns):
                    if isinstance(t, dict) and t.get("text"):
                        enc.ingest_turn(sk, t.get("speaker", "?"), t["text"],
                                        observed_at=date + timedelta(minutes=ti))
            enc.close()
            c1 = st.counts(schema)
            print(f"[{idx}] encoded {c1['raw_turns']} turns -> {c1['episodic']} events "
                  f"in {time.perf_counter()-t0:.0f}s", flush=True)

            # --- CONSOLIDATE (B2) ---
            t1 = time.perf_counter()
            cres = Consolidator(st, schema, ns, cli, args.model, embed_fn, meter=meter,
                                workers=args.workers).run()
            print(f"[{idx}] consolidated {cres} in {time.perf_counter()-t1:.0f}s ({meter.summary()})", flush=True)

            # --- QA: recency-gated dual retrieval (B3) ---
            pool = Queue()
            for _ in range(args.workers):
                s2 = BrainStore(DSN)
                pool.put(Retriever(s2, schema, ns, embed_fn, reranker_model=args.reranker, rerank_lock=rlock))
            def do_qa(q):
                question, expected = q.get("question", ""), str(q.get("answer", ""))
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                if not question or not expected:
                    return None
                R = pool.get()
                try:
                    rows = R.retrieve(question, k=args.k, top_k=args.top_k)
                finally:
                    pool.put(R)
                ctx = context_block(rows, max_chars=args.max_chars)
                pred = answer_from_ctx(cli, args.model, question, ctx, meter, relaxed=True)
                return cat, judge(cli, args.judge_model, question, expected, pred, meter)
            qa = sample.get("qa", [])
            t2 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for r in ex.map(do_qa, qa):
                    if r:
                        results.append(r)
            while not pool.empty():
                pool.get().store.conn.close()
            print(f"[{idx}] answered {len(qa)} QA in {time.perf_counter()-t2:.0f}s ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== BRAIN ENGINE (B1+B2+B3) — LoCoMo ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
