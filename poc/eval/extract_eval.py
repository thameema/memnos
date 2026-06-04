"""Extraction eval: run a model over the gold set, score entity-F1 + fact-F1, time it.

Default model: local Ollama qwen2.5:7b (constrained JSON, free, private).
Optional: OpenAI gpt-4o-mini — only runs with --openai AND OPENAI_API_KEY set
(spends a few cents; opt-in by design).

Usage:
    python eval/extract_eval.py                 # qwen2.5:7b (local)
    python eval/extract_eval.py --model qwen2.5:14b
    python eval/extract_eval.py --openai        # also run gpt-4o-mini
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, ".")
import httpx
from eval.gold import GOLD

SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "object",
            "properties": {"name": {"type": "string"}, "type": {"type": "string"}},
            "required": ["name", "type"]}},
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
    r = httpx.post("http://localhost:11434/api/chat", timeout=120, json={
        "model": model, "stream": False, "format": SCHEMA,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": prompt(item)}],
        "options": {"temperature": 0},
    })
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def run_openai(model, item):
    from openai import OpenAI
    cli = OpenAI()
    r = cli.chat.completions.create(model=model, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYS + " Schema: " + json.dumps(SCHEMA)},
                  {"role": "user", "content": prompt(item)}])
    return json.loads(r.choices[0].message.content)


def _norm(s):
    return "".join(c for c in (s or "").lower().strip() if c.isalnum() or c == " ").strip()


def score(pred, gold):
    # entities: set overlap on normalized names
    pe = {_norm(e.get("name", "")) for e in pred.get("entities", []) if e.get("name")}
    ge = {_norm(x) for x in gold["entities"]}
    etp = len(pe & ge)
    # facts: lenient match — a gold fact is hit if some predicted fact shares
    # normalized subject AND object (predicate phrasing varies too much to be strict)
    pf = {(_norm(f.get("subject", "")), _norm(f.get("object", ""))) for f in pred.get("facts", [])}
    gf = {(_norm(s), _norm(o)) for s, _, o in gold["facts"]}
    ftp = len(pf & gf)
    return {"e_tp": etp, "e_pred": len(pe), "e_gold": len(ge),
            "f_tp": ftp, "f_pred": len(pf), "f_gold": len(gf)}


def prf(tp, pred, gold):
    p = tp / pred if pred else 0.0
    r = tp / gold if gold else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def evaluate(name, runner, model):
    agg = {"e_tp": 0, "e_pred": 0, "e_gold": 0, "f_tp": 0, "f_pred": 0, "f_gold": 0}
    lat = []
    fails = 0
    for item in GOLD:
        try:
            t = time.perf_counter(); pred = runner(model, item); lat.append((time.perf_counter()-t)*1000)
            for k, v in score(pred, item).items():
                agg[k] += v
        except Exception as e:
            fails += 1
            print(f"  ! {model} failed on {item['text'][:40]!r}: {e}")
    ep, er, ef = prf(agg["e_tp"], agg["e_pred"], agg["e_gold"])
    fp, fr, ff = prf(agg["f_tp"], agg["f_pred"], agg["f_gold"])
    lat.sort()
    print(f"\n=== {name} ({model}) — {len(GOLD)-fails}/{len(GOLD)} ok ===")
    print(f"  entities  P={ep:.2f} R={er:.2f} F1={ef:.2f}")
    print(f"  facts     P={fp:.2f} R={fr:.2f} F1={ff:.2f}   (lenient: subject+object)")
    if lat:
        print(f"  latency   p50={lat[len(lat)//2]:.0f}ms  max={lat[-1]:.0f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--openai", action="store_true")
    args = ap.parse_args()

    evaluate("Local/Ollama", run_ollama, args.model)

    if args.openai:
        if not os.environ.get("OPENAI_API_KEY"):
            print("\n[skip] OpenAI arm: set OPENAI_API_KEY to run gpt-4o-mini (~a few cents).")
        else:
            evaluate("OpenAI", run_openai, "gpt-4o-mini")


if __name__ == "__main__":
    main()
