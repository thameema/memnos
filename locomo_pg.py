"""LoCoMo benchmark driver on the PG engine — ingest-once per sample, answer over
retrieved context, judge, score by category. Local embeddings (free); OpenAI for
extract/answer/judge, all metered with a hard --budget cap (the v10 safety rail).

Usage (cheap smoke):
    OPENAI_API_KEY=... python locomo_pg.py --sample-ids 0 --max-qa 20 --budget 1.00
"""
import argparse
import json
import sys
import time

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_core import ingest as ing
from memnos_core import retrieve as ret
from memnos_core.embedders import OpenAIEmbedder, LocalEmbedder
from memnos_core.storage import PgStorage
from memnos_core.usage import CostMeter, BudgetExceeded

DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "adversarial"}


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def ingest_sample(st, schema, ns, sample, cli, model, meter, do_extract, embed_fn):
    conv = sample.get("conversation", {})
    sess_keys = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                       key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
    n = 0
    for sk in sess_keys:
        turns = conv[sk]
        if not isinstance(turns, list):
            continue
        for t in turns:
            if not isinstance(t, dict):
                continue
            text = t.get("text", "")
            spk = t.get("speaker", "?")
            if not text:
                continue
            ing.ingest_turn(st, schema, ns, "user", f"{spk}: {text}", embed_fn=embed_fn,
                            openai_client=cli, extract_model=model, meter=meter, do_extract=do_extract)
            n += 1
    return n


def answer(st, schema, ns, q, cli, model, meter, embed_fn):
    rows = ret.retrieve(st, schema, ns, q, embed_fn=embed_fn, k=20, top_k=8)
    ctx = ret.context_block(rows)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="0")
    ap.add_argument("--max-qa", type=int, default=20)
    ap.add_argument("--budget", type=float, default=1.00)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--local-embed", action="store_true", help="use local bge-small (384-d) instead of OpenAI")
    args = ap.parse_args()

    cli = OpenAI()
    meter = CostMeter(budget_usd=args.budget)
    embedder = LocalEmbedder() if args.local_embed else OpenAIEmbedder(cli, meter)
    embed_fn = embedder.embed
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
            t0 = time.perf_counter()
            n = ingest_sample(st, schema, ns, sample, cli, args.model, meter, not args.no_extract, embed_fn)
            print(f"[{idx}] ingested {n} turns in {time.perf_counter()-t0:.0f}s   ({meter.summary()})")

            qa = sample.get("qa", [])[: args.max_qa] if args.max_qa else sample.get("qa", [])
            for q in qa:
                question, expected = q.get("question", ""), str(q.get("answer", ""))
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                if not question or not expected:
                    continue
                pred = answer(st, schema, ns, question, cli, args.model, meter, embed_fn)
                sc = judge(cli, args.model, question, expected, pred, meter)
                results.append((cat, sc))
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    # score by category
    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== LoCoMo (PG engine) ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
