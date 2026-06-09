"""Judge audit + failure taxonomy for the memnos LoCoMo eval.

Reuses ALREADY-INGESTED tenant_pa{idx} schemas (no re-ingest cost). For each QA it
runs the PRODUCTION recall->context->answer path, then records a full taxonomy so we
can see WHERE accuracy is lost:

  base_verdict   : the current judge ("convey same key info", gpt-4o)
  careful_verdict: a stricter, abstention-aware re-judge (catches judge false-positives)
  lenient_verdict: an explicitly forgiving judge (catches judge false-negatives)
  in_context     : is the expected answer string present in the retrieved context?
                   -> separates RETRIEVAL failure (not present) from SYNTHESIS failure
                      (present but answered wrong)

Outputs per-category rates + a judge-disagreement summary + dumps every row to
/tmp/memnos_audit.json for eyeballing. Usage: OPENAI_API_KEY=... python memnos_judge_audit.py
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")


def _load_env(path=".env"):
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()

import httpx
from openai import OpenAI
from memnos_brain import BrainStore, rerank as brain_rerank
from memnos_brain.service import MemnosMemory
from locomo_pg_qa_only import answer_from_ctx
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP

DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def _norm(s):
    return " ".join(str(s).lower().split())


def _present(expected, ctx):
    """Lenient 'is the answer in the context' check: every comma-separated reference
    token appears (handles list answers like 'Spain, England')."""
    c = _norm(ctx)
    parts = [_norm(p) for p in str(expected).replace(";", ",").split(",") if _norm(p)]
    if not parts:
        return False
    return all(p in c for p in parts)


def _judge(cli, model, q, expected, pred, meter, style):
    if style == "base":
        prompt = (f"Question: {q}\nReference answer: {expected}\nModel answer: {pred}\n"
                  "Does the model answer convey the same key information as the reference "
                  "(ignore wording/format)? Reply YES or NO.")
    elif style == "careful":
        prompt = (f"Question: {q}\nReference answer: {expected}\nModel answer: {pred}\n"
                  "Grade strictly. Reply YES only if the model answer is factually correct AND "
                  "covers ALL key items in the reference (for list answers, every item). Reply NO "
                  "if it is wrong, only partially correct, evasive, or says it doesn't know.")
    else:  # lenient
        prompt = (f"Question: {q}\nReference answer: {expected}\nModel answer: {pred}\n"
                  "Be forgiving. Reply YES if the model answer is on the right track / partially "
                  "correct / a synonym or paraphrase, even if incomplete. Reply NO only if it is "
                  "clearly wrong or contentless.")
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=4,
        messages=[{"role": "user", "content": prompt}])
    meter.record("judge", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    return 1 if r.choices[0].message.content.strip().upper().startswith("YES") else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter()
    from validate_brain import CachedEmbedder
    embed = CachedEmbedder(cli, meter)
    brain_rerank.rerank("warm", ["a", "b"])
    data = httpx.get(URL, follow_redirects=True, timeout=60).json()
    st = BrainStore(DSN)

    rows = []
    for idx in ids:
        sample = data[idx]; sid = str(sample.get("sample_id", idx)); ns = f"locomo:{sid}"
        schema = f"tenant_pa{idx}"
        with st.conn.cursor() as c:
            c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
            if not c.fetchone()["exists"]:
                print(f"[skip] {schema} not ingested — run phaseA_recombine.py first"); continue
        mem = MemnosMemory(st, embed, dim=embed.dim, llm=cli); mem.schema = schema
        qa = [q for q in sample.get("qa", []) if q.get("question") and str(q.get("answer", "")) != ""]
        # sequential engine calls (one conn / reranker) -> contexts
        ctxs = []
        for q in qa:
            ctxs.append(mem.context(ns, q["question"]))
        # parallel answer + 3-way judge
        def work(pair):
            q, ctx = pair
            ques, exp = q["question"], str(q["answer"])
            cat = CATEGORY_MAP.get(q.get("category"), "unknown")
            pred = answer_from_ctx(cli, args.model, ques, ctx, meter, relaxed=True)
            return {"sample": idx, "cat": cat, "q": ques, "expected": exp, "pred": pred,
                    "in_context": _present(exp, ctx),
                    "base": _judge(cli, args.judge_model, ques, exp, pred, meter, "base"),
                    "careful": _judge(cli, args.judge_model, ques, exp, pred, meter, "careful"),
                    "lenient": _judge(cli, args.judge_model, ques, exp, pred, meter, "lenient")}
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            rows.extend(ex.map(work, list(zip(qa, ctxs))))
        print(f"[{idx}] audited {len(qa)} ({meter.summary()})", flush=True)

    with open("/tmp/memnos_audit.json", "w") as fh:
        json.dump(rows, fh, indent=1)

    n = len(rows)
    def rate(key, subset=None):
        s = subset or rows
        return (sum(r[key] for r in s) / len(s)) if s else 0.0

    print(f"\n=== JUDGE AUDIT (n={n}) ===")
    print(f"  base judge OVERALL   : {rate('base'):.0%}")
    print(f"  careful judge OVERALL: {rate('careful'):.0%}   (strict / all-items)")
    print(f"  lenient judge OVERALL: {rate('lenient'):.0%}   (forgiving / partial-ok)")
    print(f"  => judge band: {rate('careful'):.0%} - {rate('lenient'):.0%} (spread = judge subjectivity)")

    print("\n  per-category (base | careful | lenient):")
    for cat in sorted(set(r["cat"] for r in rows)):
        s = [r for r in rows if r["cat"] == cat]
        print(f"    {cat:<12} {rate('base',s):.0%} | {rate('careful',s):.0%} | {rate('lenient',s):.0%}   (n={len(s)})")

    # failure taxonomy on BASE failures
    fails = [r for r in rows if not r["base"]]
    retr = [r for r in fails if not r["in_context"]]
    synth = [r for r in fails if r["in_context"]]
    jfn = [r for r in fails if r["lenient"]]   # base said NO but lenient said YES => judge false-negative
    print(f"\n=== FAILURE TAXONOMY (base failures n={len(fails)}) ===")
    print(f"  RETRIEVAL miss (answer NOT in context): {len(retr)} ({len(retr)/max(1,len(fails)):.0%})")
    print(f"  SYNTHESIS miss (answer IN context, wrong): {len(synth)} ({len(synth)/max(1,len(fails)):.0%})")
    print(f"  JUDGE false-neg (lenient would pass)     : {len(jfn)} ({len(jfn)/max(1,len(fails)):.0%})")
    print(f"\n  -> full dump: /tmp/memnos_audit.json ({meter.summary()})")


if __name__ == "__main__":
    main()
