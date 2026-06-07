"""Cross-provider LoCoMo eval — pluggable ANSWERER + multi-judge (de-biased).

Why: GPT judging GPT's own answers has self-preference bias. Using an INDEPENDENT
provider as judge (Claude via the `claude -p` CLI — rides existing CLI auth, no API key)
gives a fairer, "locked" score. This harness lets us mix providers:
  --answerer  gpt-4o-mini | gpt-5-mini | gpt-5 | claude
  --judges    gpt-4o,claude            (scores under EACH, cross-provider)

Reuses ALREADY-INGESTED tenant_pa{idx} schemas (no re-ingest). Builds context via the
PRODUCTION recall path, answers, then judges with every judge. Saves predictions to
/tmp/xprov_preds.json. Usage: OPENAI_API_KEY=... python cross_provider_eval.py --answerer gpt-5-mini
"""
import argparse
import json
import os
import re
import subprocess
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
from collections import defaultdict
from openai import OpenAI
from memnos_brain import BrainStore, rerank as brain_rerank
from memnos_brain.service import MemnosMemory
from validate_brain import CachedEmbedder
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP
from locomo_pg_qa_only import RELAXED_SYS

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
_GPT5 = ("gpt-5", "gpt-5-mini", "gpt-5-codex")

JUDGE_PROMPT = ("Question: {q}\nReference answer: {exp}\nModel answer: {pred}\n"
                "Does the model answer convey the same key information as the reference "
                "(ignore wording/format; for list answers it must cover all items)? "
                "Reply with ONLY the word YES or NO.")


def claude_cli(prompt, timeout=60):
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def answer(cli, model, q, ctx):
    msgs = [{"role": "system", "content": RELAXED_SYS},
            {"role": "user", "content": f"Memories:\n{ctx}\n\nQuestion: {q}\nAnswer:"}]
    if model == "claude":
        return claude_cli(f"{RELAXED_SYS}\n\nMemories:\n{ctx}\n\nQuestion: {q}\nAnswer concisely:")
    if model in _GPT5:
        r = cli.chat.completions.create(model=model, max_completion_tokens=1500, messages=msgs)
    else:
        r = cli.chat.completions.create(model=model, temperature=0, max_tokens=120, messages=msgs)
    return r.choices[0].message.content.strip()


def judge_one(cli, judge, q, exp, pred):
    p = JUDGE_PROMPT.format(q=q, exp=exp, pred=pred)
    if judge == "claude":
        out = claude_cli(p)
    else:
        r = cli.chat.completions.create(model=judge, temperature=0, max_tokens=4,
                                        messages=[{"role": "user", "content": p}])
        out = r.choices[0].message.content
    return 1 if out.strip().upper().startswith("YES") else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answerer", default="gpt-4o-mini")
    ap.add_argument("--judges", default="gpt-4o,claude")
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]

    cli = OpenAI(max_retries=5)
    embed = CachedEmbedder(cli, TSCostMeter())
    brain_rerank.rerank("warm", ["a", "b"])
    data = httpx.get(URL, follow_redirects=True, timeout=60).json()
    st = BrainStore(DSN)

    preds = []
    for idx in ids:
        sample = data[idx]; sid = str(sample.get("sample_id", idx)); ns = f"locomo:{sid}"
        schema = f"tenant_pa{idx}"
        mem = MemnosMemory(st, embed, dim=embed.dim, llm=cli); mem.schema = schema
        qa = [q for q in sample.get("qa", []) if q.get("question") and str(q.get("answer", "")) != ""]
        items = [(q, mem.context(ns, q["question"])) for q in qa]

        def do(it):
            q, ctx = it
            ques, exp = q["question"], str(q["answer"])
            cat = CATEGORY_MAP.get(q.get("category"), "?")
            try:
                pred = answer(cli, args.answerer, ques, ctx)
            except Exception as e:
                pred = f"[err {type(e).__name__}]"
            row = {"sample": idx, "cat": cat, "q": ques, "expected": exp, "pred": pred}
            for j in judges:
                try:
                    row[f"j_{j}"] = judge_one(cli, j, ques, exp, pred)
                except Exception:
                    row[f"j_{j}"] = 0
            return row
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            preds.extend(ex.map(do, items))
        print(f"[{idx}] {len(items)} answered+judged", flush=True)

    with open("/tmp/xprov_preds.json", "w") as fh:
        json.dump({"answerer": args.answerer, "judges": judges, "rows": preds}, fh, indent=1)

    print(f"\n=== ANSWERER = {args.answerer} ===")
    for j in judges:
        key = f"j_{j}"
        agg = defaultdict(lambda: [0, 0])
        for r in preds:
            agg[r["cat"]][0] += r[key]; agg[r["cat"]][1] += 1
            agg["ALL"][0] += r[key]; agg["ALL"][1] += 1
        line = "  ".join(f"{c}={100*agg[c][0]/agg[c][1]:.0f}%" for c in
                         ["single_hop", "multi_hop", "temporal", "open_domain", "ALL"] if c in agg)
        print(f"  judge={j:<10} {line}")
    print("  -> preds: /tmp/xprov_preds.json")


if __name__ == "__main__":
    main()
