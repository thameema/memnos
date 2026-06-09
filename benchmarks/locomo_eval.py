"""LoCoMo end-to-end benchmark — FROM SCRATCH, on the single `core` engine.

Per conversation: create a fresh schema, ENCODE every turn (event-segmented episodic
memory + 1536-d embeddings), CONSOLIDATE into semantic facts + entity dossiers
(gpt-4o-mini), then for each QA: retrieve (recency-gated dual + cross-encoder rerank,
NO LLM at query time) -> answer with the configured answerer -> judge. Budget-capped.

This is the locked config (see LOCKED_BASELINE.md): gpt-4o-mini extract + text-embedding-
3-small (1536-d) + gpt-5-mini answer + gpt-4o judge. Same `core` engine as production.

Usage (full 10 conversations, n=1542):
    OPENAI_API_KEY=...  MEMNOS_DSN=postgresql://...  \
    python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9 --budget 25
"""
import argparse
import json
import os
import os.path
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from openai import OpenAI

from core import BrainStore, Encoder, Consolidator
from core import rerank as brain_rerank
from core.service import MemnosMemory   # production recall path (entity-guarantee + timeline arms)
from core.usage import BudgetExceeded
from _harness import CachedEmbedder, TSCostMeter, CATEGORY_MAP

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
_GPT5 = ("gpt-5", "gpt-5-mini", "gpt-5-codex")

RELAXED_SYS = ("Use the retrieved memories as your primary evidence and reason over them "
               "to answer. It is fine to make a small, well-supported inference from the "
               "memories. Give your best concise answer (a short phrase). Only say "
               "'Not mentioned' if there is truly no relevant information.")
JUDGE_PROMPT = ("Question: {q}\nReference answer: {exp}\nModel answer: {pred}\n"
                "Does the model answer convey the same key information as the reference "
                "(ignore wording/format; for list answers it must cover all items)? "
                "Reply with ONLY the word YES or NO.")

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


def answer(cli, model, q, ctx, meter):
    msgs = [{"role": "system", "content": RELAXED_SYS},
            {"role": "user", "content": f"Memories:\n{ctx}\n\nQuestion: {q}\nAnswer:"}]
    if model in _GPT5:        # gpt-5 family: no temperature, use max_completion_tokens
        r = cli.chat.completions.create(model=model, max_completion_tokens=1500, messages=msgs)
    else:
        r = cli.chat.completions.create(model=model, temperature=0, max_tokens=120, messages=msgs)
    if r.usage:
        meter.record("answer", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return (r.choices[0].message.content or "").strip()


def judge_one(cli, model, q, exp, pred, meter):
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=4,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(q=q, exp=exp, pred=pred)}])
    if r.usage:
        meter.record("judge", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return 1 if (r.choices[0].message.content or "").strip().upper().startswith("YES") else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="0,1,2,3,4,5,6,7,8,9")  # full LoCoMo by default
    ap.add_argument("--extractor", default="gpt-4o-mini")   # fact extraction / consolidation
    ap.add_argument("--answerer", default="gpt-5-mini")     # the calling agent in production
    ap.add_argument("--judge", default="gpt-4o")
    ap.add_argument("--reranker", default="BAAI/bge-reranker-base")
    ap.add_argument("--k", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=14)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=25.0)
    ap.add_argument("--max-qa", type=int, default=0)        # 0 = all QA
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",") if x.strip() != ""]

    cli = OpenAI(max_retries=5, timeout=60)
    meter = TSCostMeter(budget_usd=args.budget)
    embed = CachedEmbedder(cli, meter)
    rlock = threading.Lock()
    print(f"warming reranker {args.reranker} ...", flush=True)
    brain_rerank.rerank("warm", ["a", "b"], args.reranker)
    data = load_dataset()
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    st = BrainStore(DSN)
    rows_out = []

    try:
        for idx in ids:
            sample = data[idx]; sid = str(sample.get("sample_id", idx))
            tenant = f"locomo{idx}"; schema = f"tenant_{tenant}"; ns = f"locomo:{sid}"
            st.drop_schema(tenant); st.create_schema(tenant, dim=embed.dim)

            # --- ENCODE ---
            conv = sample.get("conversation", {})
            sess = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                          key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
            embed.prime([t["text"] for sk in sess for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                         if isinstance(t, dict) and t.get("text")])
            enc = Encoder(st, schema, ns, embed)
            t0 = time.perf_counter()
            for si, sk in enumerate(sess):
                date = parse_date(str(conv.get(f"{sk}_date_time", "")), base + timedelta(days=30 * si))
                for ti, t in enumerate(conv[sk] if isinstance(conv.get(sk), list) else []):
                    if isinstance(t, dict) and t.get("text"):
                        enc.ingest_turn(sk, t.get("speaker", "?"), t["text"],
                                        observed_at=date + timedelta(minutes=ti))
            enc.close()
            c1 = st.counts(schema)
            print(f"[{idx}] encoded {c1['raw_turns']} turns -> {c1['episodic']} events "
                  f"in {time.perf_counter()-t0:.0f}s", flush=True)

            # --- CONSOLIDATE ---
            t1 = time.perf_counter()
            cres = Consolidator(st, schema, ns, cli, args.extractor, embed,
                                meter=meter, workers=args.workers).run()
            print(f"[{idx}] consolidated {cres} in {time.perf_counter()-t1:.0f}s ({meter.summary()})", flush=True)

            # --- QA: retrieve via the PRODUCTION recall path (same as MCP/REST/hooks),
            #     which adds the entity-guarantee arm (multi-item/aggregation) and the
            #     temporal timeline arm on top of hybrid+rerank. One MemnosMemory (its own
            #     DB connection) per worker so reads parallelize safely. ---
            pool = Queue()
            for _ in range(args.workers):
                m = MemnosMemory(BrainStore(DSN), embed, dim=embed.dim, llm=cli)
                m.schema = schema
                pool.put(m)
            qa = [q for q in sample.get("qa", []) if q.get("question") and str(q.get("answer", "")) != ""]
            if args.max_qa:
                qa = qa[:args.max_qa]

            def do_qa(q):
                ques, exp = q["question"], str(q["answer"])
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                m = pool.get()
                try:
                    ctx = m.context(ns, ques, max_chars=args.max_chars)
                except Exception:
                    ctx = ""
                finally:
                    pool.put(m)
                try:
                    pred = answer(cli, args.answerer, ques, ctx, meter)
                except Exception as e:
                    pred = f"[err {type(e).__name__}]"
                try:
                    sc = judge_one(cli, args.judge, ques, exp, pred, meter)
                except Exception:
                    sc = 0
                return {"sample": idx, "cat": cat, "q": ques, "expected": exp, "pred": pred, "score": sc}

            t2 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                rows_out.extend(ex.map(do_qa, qa))
            while not pool.empty():
                pool.get().store.conn.close()
            print(f"[{idx}] answered+judged {len(qa)} QA in {time.perf_counter()-t2:.0f}s ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED on budget cap: {e}", flush=True)

    # --- score ---
    agg = defaultdict(lambda: [0, 0])
    for r in rows_out:
        agg[r["cat"]][0] += r["score"]; agg[r["cat"]][1] += 1
        agg["OVERALL"][0] += r["score"]; agg["OVERALL"][1] += 1
    print(f"\n=== LoCoMo — extract={args.extractor} answer={args.answerer} judge={args.judge} ===")
    summary = {}
    for c in ["single_hop", "multi_hop", "temporal", "open_domain", "OVERALL"]:
        if c in agg and agg[c][1]:
            pct = round(100 * agg[c][0] / agg[c][1])
            summary[c] = {"correct": agg[c][0], "n": agg[c][1], "pct": pct}
            print(f"  {c:<12} {agg[c][0]}/{agg[c][1]} = {pct}%")
    print(f"\n{meter.summary()}")

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "results", "locomo-latest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"config": {"extractor": args.extractor, "answerer": args.answerer,
                              "judge": args.judge, "reranker": args.reranker,
                              "sample_ids": ids, "k": args.k, "top_k": args.top_k},
                   "summary": summary, "cost": meter.summary(), "rows": rows_out}, fh, indent=1)
    print(f"  -> results: {out}")


if __name__ == "__main__":
    main()
