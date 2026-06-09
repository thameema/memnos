"""Extraction eval + cost tracking: run a model over the gold set, score
entity-F1 + fact-F1, and record token usage + USD cost per extraction.

The cost capture here mirrors what the production usage ledger records on every
real extraction (see usage_ledger note at bottom) so the UI can show per-tenant
spend and we can optimize high-cost extractions later.

Default local model: qwen2.5:14b. OpenAI arm opt-in (--openai + OPENAI_API_KEY).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, ".")
import httpx
from eval.gold import GOLD

# USD per 1M tokens (input, output) — 2026 approx; keep in sync with provider pricing
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "object",
            "properties": {"name": {"type": "string"}, "type": {"type": "string"}}, "required": ["name", "type"]}},
        "facts": {"type": "array", "items": {"type": "object",
            "properties": {"subject": {"type": "string"}, "predicate": {"type": "string"},
                           "object": {"type": "string"}, "valid_at": {"type": ["string", "null"]}},
            "required": ["subject", "predicate", "object"]}},
    },
    "required": ["entities", "facts"],
}
SYS = ("Extract a knowledge graph from the text. Return entities (name,type) and "
       "subject-predicate-object facts. Lowercase names. Resolve relative dates "
       "against SESSION DATE into valid_at (YYYY-MM-DD) or null. JSON only.")


def prompt(item):
    return f"SESSION DATE: {item['session_date']}\n\nTEXT:\n{item['text']}"


def run_ollama(model, item):
    r = httpx.post("http://localhost:11434/api/chat", timeout=300, json={
        "model": model, "stream": False, "format": SCHEMA,
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt(item)}],
        "options": {"temperature": 0}})
    r.raise_for_status()
    d = r.json()
    usage = {"in": d.get("prompt_eval_count", 0), "out": d.get("eval_count", 0), "cost": 0.0}
    return json.loads(d["message"]["content"]), usage


def run_openai(model, item):
    from openai import OpenAI
    cli = OpenAI()
    r = cli.chat.completions.create(model=model, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYS + " Schema: " + json.dumps(SCHEMA)},
                  {"role": "user", "content": prompt(item)}])
    pt, ct = r.usage.prompt_tokens, r.usage.completion_tokens
    pin, pout = PRICING.get(model, (0, 0))
    cost = pt / 1e6 * pin + ct / 1e6 * pout
    return json.loads(r.choices[0].message.content), {"in": pt, "out": ct, "cost": cost}


def _norm(s):
    return "".join(c for c in (s or "").lower().strip() if c.isalnum() or c == " ").strip()


def score(pred, gold):
    pe = {_norm(e.get("name", "")) for e in pred.get("entities", []) if e.get("name")}
    ge = {_norm(x) for x in gold["entities"]}
    pf = {(_norm(f.get("subject", "")), _norm(f.get("object", ""))) for f in pred.get("facts", [])}
    gf = {(_norm(s), _norm(o)) for s, _, o in gold["facts"]}
    return {"e_tp": len(pe & ge), "e_pred": len(pe), "e_gold": len(ge),
            "f_tp": len(pf & gf), "f_pred": len(pf), "f_gold": len(gf)}


def prf(tp, pred, gold):
    p = tp / pred if pred else 0.0
    r = tp / gold if gold else 0.0
    return p, r, (2 * p * r / (p + r) if (p + r) else 0.0)


def evaluate(name, runner, model):
    agg = {k: 0 for k in ["e_tp", "e_pred", "e_gold", "f_tp", "f_pred", "f_gold"]}
    lat, tin, tout, cost, fails = [], 0, 0, 0.0, 0
    for item in GOLD:
        try:
            t = time.perf_counter(); pred, u = runner(model, item); lat.append((time.perf_counter()-t)*1000)
            for k, v in score(pred, item).items():
                agg[k] += v
            tin += u["in"]; tout += u["out"]; cost += u["cost"]
        except Exception as e:
            fails += 1; print(f"  ! {model} failed on {item['text'][:40]!r}: {e}")
    ep, er, ef = prf(agg["e_tp"], agg["e_pred"], agg["e_gold"])
    fp, fr, ff = prf(agg["f_tp"], agg["f_pred"], agg["f_gold"])
    lat.sort()
    n = len(GOLD) - fails
    print(f"\n=== {name} ({model}) — {n}/{len(GOLD)} ok ===")
    print(f"  entities  P={ep:.2f} R={er:.2f} F1={ef:.2f}")
    print(f"  facts     P={fp:.2f} R={fr:.2f} F1={ff:.2f}   (lenient: subject+object)")
    if lat:
        print(f"  latency   p50={lat[len(lat)//2]:.0f}ms")
    print(f"  tokens    in={tin} out={tout}  ({tin//max(n,1)} in / {tout//max(n,1)} out per doc)")
    if cost > 0:
        print(f"  COST      ${cost:.5f} total  =  ${cost/max(n,1):.6f}/doc  =  ${cost/max(n,1)*1000:.2f}/1k extractions")
    else:
        print(f"  COST      $0 (local)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:14b")
    ap.add_argument("--openai", action="store_true")
    args = ap.parse_args()
    evaluate("Local/Ollama", run_ollama, args.model)
    if args.openai:
        if not os.environ.get("OPENAI_API_KEY"):
            print("\n[skip] OpenAI arm: set OPENAI_API_KEY to run gpt-4o-mini.")
        else:
            evaluate("OpenAI", run_openai, "gpt-4o-mini")


if __name__ == "__main__":
    main()

# --- production note: usage ledger ---------------------------------------
# In production, every extraction writes a row to a `usage` table:
#   (tenant, ts, operation='extract', model, prompt_tokens, completion_tokens, cost_usd, episode_id)
# computed exactly as run_openai() does above (provider usage * PRICING). The
# UI then aggregates per-tenant/per-period spend and surfaces the priciest
# extractions so they can be tuned (gate, batch, smaller model, or go local).
