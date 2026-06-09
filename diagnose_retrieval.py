"""Root-cause diagnostic: is accuracy lost at RETRIEVAL or at ANSWERING?

LoCoMo QA items carry an `evidence` field = the dialogue turn ids (dia_id, e.g.
"D1:2") that support the gold answer. We resolve those to their turn text, run
our normal retrieval for the question, and check whether the supporting evidence
is actually in what we retrieved (recall@k). This cleanly separates:
  - low evidence-recall  -> RETRIEVAL/ingest problem (we never surfaced the memory)
  - high recall but wrong -> ANSWER/extraction/judge problem

Pure embeddings (query) + DB reads — no answer/judge LLM, so it's cheap.

Usage: OPENAI_API_KEY=... python diagnose_retrieval.py --sample-ids all --top-k 30
"""
import argparse
import sys
sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_core import local_models
from memnos_core.embedders import OpenAIEmbedder
from memnos_core.storage import PgStorage
from memnos_core.usage import CostMeter

DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "adversarial"}


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def build_diaid_map(sample):
    """dia_id -> turn text, across all sessions."""
    conv = sample.get("conversation", {})
    m = {}
    for k, v in conv.items():
        if not (k.startswith("session_") and isinstance(v, list)):
            continue
        for t in v:
            if isinstance(t, dict) and t.get("dia_id"):
                m[t["dia_id"]] = t.get("text", "")
    return m


def norm(s):
    return " ".join(str(s).lower().split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="all")
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--top-k", type=int, default=30, help="how many retrieved mems counted (after rerank)")
    ap.add_argument("--rerank", action="store_true", help="apply cross-encoder rerank before counting top-k")
    args = ap.parse_args()
    ids = list(range(10)) if args.sample_ids == "all" else [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    meter = CostMeter()
    embed_fn = OpenAIEmbedder(cli, meter).embed
    if args.rerank:
        local_models.rerank("warm", ["a", "b"])
    data = load_dataset()
    st = PgStorage(DSN)

    # per-category tallies
    cat_q = {}; cat_full = {}; cat_any = {}; n_evid = 0; n_noevid = 0
    for idx in ids:
        sample = data[idx]
        sid = str(sample.get("sample_id", idx))
        schema = f"tenant_locomo{idx}"
        ns = f"locomo:{sid}"
        with st.conn.cursor() as c:
            c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
            if not c.fetchone()["exists"]:
                print(f"[{idx}] skip — {schema} missing"); continue
        dmap = build_diaid_map(sample)
        for q in sample.get("qa", []):
            question = q.get("question", "")
            cat = CATEGORY_MAP.get(q.get("category"), "unknown")
            ev = q.get("evidence", []) or []
            # resolve evidence dia_ids -> texts
            ev_texts = [norm(dmap.get(e, "")) for e in ev if isinstance(e, str) and dmap.get(e)]
            ev_texts = [t for t in ev_texts if t]
            if not question:
                continue
            if not ev_texts:
                n_noevid += 1
                continue
            n_evid += 1
            qvec = embed_fn(question)
            cands = st.hybrid_search(schema, ns, question, qvec, k=args.k, top_k=args.k)
            if args.rerank and len(cands) > 1:
                order = local_models.rerank(question, [c["content"] for c in cands])
                cands = [cands[i] for i, _ in order]
            retrieved = " || ".join(norm(c["content"]) for c in cands[: args.top_k])
            hits = sum(1 for t in ev_texts if t in retrieved)
            cat_q[cat] = cat_q.get(cat, 0) + 1
            cat_full[cat] = cat_full.get(cat, 0) + (1 if hits == len(ev_texts) else 0)
            cat_any[cat] = cat_any.get(cat, 0) + (1 if hits > 0 else 0)

    print(f"\n=== RETRIEVAL RECALL@{args.top_k} (evidence in retrieved set) "
          f"rerank={args.rerank} ===")
    print(f"{'category':<12} {'n':>5} {'full-recall':>12} {'any-recall':>11}")
    tq = tf = ta = 0
    for cat in sorted(cat_q):
        n = cat_q[cat]; f = cat_full[cat]; a = cat_any[cat]
        tq += n; tf += f; ta += a
        print(f"{cat:<12} {n:>5} {f:>6}/{n} {100*f/n:>4.0f}% {a:>5}/{n} {100*a/n:>3.0f}%")
    if tq:
        print(f"{'ALL':<12} {tq:>5} {tf:>6}/{tq} {100*tf/tq:>4.0f}% {ta:>5}/{tq} {100*ta/tq:>3.0f}%")
    print(f"\n(questions with usable evidence: {n_evid}; without: {n_noevid})")
    print("READ: full-recall = ALL evidence turns retrieved; any-recall = >=1 retrieved.")
    print("If recall is HIGH but answer accuracy LOW -> answer/extraction problem.")
    print("If recall is LOW -> retrieval/ingest problem (the ceiling on accuracy).")
    print(f"\nquery-embed cost: {meter.summary()}")


if __name__ == "__main__":
    main()
