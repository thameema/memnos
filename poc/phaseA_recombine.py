"""LoCoMo benchmark driver — runs THE PRODUCTION ENGINE (memnos_brain.MemnosMemory).

This is deliberately a THIN driver: it does NOT reimplement ingest/retrieve. It calls
the exact same MemnosMemory.ingest_session / consolidate / recall / context that the
production server (memnos_server.py) serves. Whatever this scores IS production's score
— there is ONE engine, not a benchmarked copy and a shipped copy.

Per sample:
  INGEST   mem.ingest_session(per session, chronological)  -> SPO facts + supersession + graph
  DISTILL  mem.consolidate(ns)                              -> entity dossiers
  RETRIEVE mem.context(ns, question)                        -> recall + rerank + timeline (NO query LLM)
  ANSWER   answer_from_ctx (the APP's LLM, not memnos)      -> judged by gpt-4o (LLM-as-judge)

Engine calls run sequentially (one DB connection / reranker — exactly the logic the
server runs per request); only the stateless answer+judge LLM calls are parallelized.

Usage: OPENAI_API_KEY=... python phaseA_recombine.py --sample-ids 2,3,4 --budget 5
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_brain import BrainStore, rerank as brain_rerank
from memnos_brain.service import MemnosMemory
from locomo_pg_qa_only import answer_from_ctx, judge
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP
from memnos_poc.usage import BudgetExceeded
from validate_brain import CachedEmbedder, parse_date

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def ingest_via_engine(mem, embed_fn, ns, sample, claude_extract=False, workers=6):
    """Build per-session turn lists and feed them to the PRODUCTION ingest path.

    claude_extract=True runs the COST-HEAVY extraction on the Claude CLI (free via sub),
    PARALLEL across sessions (subprocess releases the GIL), then writes facts via the same
    engine write path. Otherwise uses the engine's built-in (OpenAI) extraction."""
    conv = sample.get("conversation", {})
    sess = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                  key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    embed_fn.prime([t["text"] for sk in sess for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                    if isinstance(t, dict) and t.get("text")])
    totals = {"turns": 0, "facts": 0, "superseded": 0}
    dated = []
    for si, sk in enumerate(sess):
        date = parse_date(str(conv.get(f"{sk}_date_time", "")), base + timedelta(days=30 * si))
        turns = [(t.get("speaker", "?"), t["text"]) for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                 if isinstance(t, dict) and t.get("text")]
        dated.append((date, sk, turns))
    dated.sort(key=lambda x: x[0] or datetime(1970, 1, 1, tzinfo=timezone.utc))

    if not claude_extract:
        for date, sk, turns in dated:
            out = mem.ingest_session(ns, turns, session_date=date, session_id=sk)
            for kk in totals:
                totals[kk] += out.get(kk, 0)
        return totals

    # --- Claude CLI extractor: parallel-extract sessions, then write in chrono order ---
    import claude_cli
    def ex(item):
        date, sk, turns = item
        content = f"SESSION DATE: {date}\n\n" + "\n".join(f"{s}: {t}" for s, t in turns if t)
        try:
            facts = claude_cli.extract(content, date)
        except Exception:
            facts = []
        return date, sk, turns, facts
    with ThreadPoolExecutor(max_workers=workers) as exr:
        extracted = list(exr.map(ex, dated))
    extracted.sort(key=lambda x: x[0] or datetime(1970, 1, 1, tzinfo=timezone.utc))   # chrono write
    for date, sk, turns, facts in extracted:
        mem.ingest_session(ns, turns, session_date=date, session_id=sk, extract=False)  # raw turns only
        totals["turns"] += len(turns)
        for f in facts:
            df, ds = mem._write_fact(ns, f, date)
            totals["facts"] += df; totals["superseded"] += ds
    return totals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--reranker", default="BAAI/bge-reranker-base")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=5.0)
    ap.add_argument("--extractor", default="openai", choices=["openai", "claude"])
    ap.add_argument("--ingest-only", action="store_true")
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=4, timeout=60)
    meter = TSCostMeter(budget_usd=args.budget)
    embed_fn = CachedEmbedder(cli, meter)
    print(f"warming reranker {args.reranker} ...", flush=True)
    brain_rerank.rerank("warm", ["a", "b"], args.reranker)
    data = load_dataset()
    results = []
    st = BrainStore(DSN)
    try:
        for idx in ids:
            sample = data[idx]; sid = str(sample.get("sample_id", idx))
            tenant = f"pa{idx}"; schema = f"tenant_{tenant}"; ns = f"locomo:{sid}"
            st.drop_schema(tenant); st.create_schema(tenant, dim=embed_fn.dim)
            # THE PRODUCTION ENGINE — same class the server instantiates per request.
            # on_usage feeds extraction/consolidation tokens to the budget meter so the
            # hard cap is authoritative (no untracked LLM spend).
            mem = MemnosMemory(st, embed_fn, dim=embed_fn.dim, llm=cli,
                               extract_model=args.model, reranker_model=args.reranker,
                               on_usage=lambda model, pt, ct: meter.record("extract", model, pt, ct))
            mem.schema = schema   # isolate benchmark data per sample (NOT production tenant_memnos)

            t0 = time.perf_counter()
            ing = ingest_via_engine(mem, embed_fn, ns, sample,
                                    claude_extract=(args.extractor == "claude"), workers=args.workers)
            dos = mem.consolidate(ns)
            print(f"[{idx}] ingested[{args.extractor}] {ing} + {dos} in {time.perf_counter()-t0:.0f}s ({meter.summary()})", flush=True)
            if args.ingest_only:
                continue

            # --- build context per question via PRODUCTION recall (sequential = server logic) ---
            qa = [q for q in sample.get("qa", []) if q.get("question") and str(q.get("answer", "")) != ""]
            t1 = time.perf_counter()
            items = []
            for q in qa:
                question, expected = q["question"], str(q["answer"])
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                ctx = mem.context(ns, question)        # <-- production recall + rerank + timeline
                items.append((cat, question, expected, ctx))
            print(f"[{idx}] built {len(items)} contexts in {time.perf_counter()-t1:.0f}s", flush=True)

            # --- answer + judge (stateless LLM calls — safe to parallelize) ---
            def do_qa(item):
                cat, question, expected, ctx = item
                pred = answer_from_ctx(cli, args.model, question, ctx, meter, relaxed=True)
                return cat, judge(cli, args.judge_model, question, expected, pred, meter)
            t2 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for r in ex.map(do_qa, items):
                    if r:
                        results.append(r)
            print(f"[{idx}] answered {len(items)} QA in {time.perf_counter()-t2:.0f}s ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== PRODUCTION ENGINE — LoCoMo ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
