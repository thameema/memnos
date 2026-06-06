"""Phase A — recombine our PROVEN category wins into ONE no-query-LLM engine.

Diagnosis (from git history): old ArcadeDB engine won temporal (~90%) via DATE-AWARE
SESSION EXTRACTION (prepend session date → facts carry absolute dates) and multi-hop
(62%) via granular facts; our new PG engine won open-domain (70%) via RAW TURNS. Neither
combined them. Phase A does:

  INGEST (offline LLM): raw turns (observed_at = session date)  +  per-session date-aware
    fact extraction (relative dates → absolute)  +  capped entity dossiers (offline
    multi-hop pre-join).
  RETRIEVE (NO query LLM): hybrid over raw_turns ⊕ semantic, recency-gated fusion, bge
    rerank → context. (Answer LLM reads context — that's the app's LLM, not retrieval.)

Goal: lift temporal/multi-hop WITHOUT losing open-domain → past the ~56% plateau.

Usage: OPENAI_API_KEY=... python phaseA_recombine.py --sample-ids 2,3,4 --budget 3
"""
import argparse
import json
import math
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from queue import Queue

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_brain import BrainStore, rerank as brain_rerank
from memnos_poc import retrieve as ret
from locomo_pg_qa_only import answer_from_ctx, judge
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP
from memnos_poc.usage import BudgetExceeded
from validate_brain import CachedEmbedder, parse_date

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

EXTRACT_SYS = (
    "Extract atomic, self-contained FACTS from one dated session as STRUCTURED triples. "
    "RESOLVE relative dates ('yesterday','last Saturday') to ABSOLUTE using the SESSION DATE, and "
    "pronouns to named people. For each fact give: subject (the named entity it's about), predicate "
    "(a short normalized relation like 'lives_in','works_at','owns_pet','favorite_food','job_title'), "
    "object (the value), and statement (a full self-contained sentence with the date). "
    'Return JSON {"facts":[{"subject":"...","predicate":"...","object":"...","statement":"..."}]}.')
DOSSIER_SYS = (
    "Consolidate everything known about ONE subject into durable CURRENT facts, DERIVING facts "
    "that require COMBINING inputs ('A works at B' + 'B in C' => 'A works in C'). On conflict keep "
    "the most recent (dates given). Keep dates. Return JSON {\"facts\": [\"...\", ...]}.")
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def _facts(cli, model, sys_p, content, meter, op):
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=700,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": content}])
    meter.record(op, model, r.usage.prompt_tokens, r.usage.completion_tokens)
    try:
        return [str(x).strip() for x in json.loads(r.choices[0].message.content).get("facts", []) if str(x).strip()]
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


def _facts_spo(cli, model, content, meter):
    """Structured extraction: [{subject, predicate, object, statement}] — enables
    same-(subject,predicate) belief-change supersession."""
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=900,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": EXTRACT_SYS}, {"role": "user", "content": content}])
    meter.record("extract", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    out = []
    try:
        for f in json.loads(r.choices[0].message.content).get("facts", []):
            if isinstance(f, dict) and str(f.get("statement", "")).strip():
                out.append({"subject": str(f.get("subject", "")).strip(),
                            "predicate": str(f.get("predicate", "")).strip().lower().replace(" ", "_"),
                            "object": str(f.get("object", "")).strip(),
                            "statement": str(f["statement"]).strip()})
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return out


def ingest_sample(st, schema, ns, sample, cli, model, meter, embed_fn, workers,
                  max_entities=25, min_facts=3, max_dossier=6):
    conv = sample.get("conversation", {})
    sess = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                  key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)

    # prime turn embeddings (batch)
    embed_fn.prime([t["text"] for sk in sess for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                    if isinstance(t, dict) and t.get("text")])

    # --- raw turns + per-session date-aware extraction (parallel over sessions) ---
    sess_payload = []
    for si, sk in enumerate(sess):
        date = parse_date(str(conv.get(f"{sk}_date_time", "")), base + timedelta(days=30 * si))
        turns = [(t.get("speaker", "?"), t["text"]) for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                 if isinstance(t, dict) and t.get("text")]
        # store raw turns
        for ti, (spk, txt) in enumerate(turns):
            st.insert_raw_turn(schema, ns, sk, spk, txt, date + timedelta(minutes=ti), embed_fn(txt))
        sess_payload.append((date, "\n".join(f"{s}: {t}" for s, t in turns)))

    def extract(item):
        date, text = item
        content = f"SESSION DATE: {date}\n\n{text}"
        return date, _facts_spo(cli, model, content, meter)
    sess_facts = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        sess_facts = list(ex.map(extract, sess_payload))

    # write session facts (SPO) + supersede same-(subject,predicate) prior values
    ent_facts = defaultdict(list)
    all_fact_texts = [f["statement"] for _, fs in sess_facts for f in fs]
    embed_fn.prime(all_fact_texts)
    from memnos_brain.temporal import parse_event_date
    seen = set()
    n_super = 0
    # process in chronological session order so supersession closes the OLDER value
    for date, facts in sorted(sess_facts, key=lambda x: x[0] or datetime(1970, 1, 1, tzinfo=timezone.utc)):
        for f in facts:
            stmt = f["statement"]; subj = f["subject"] or None; pred = f["predicate"] or None; obj = f["object"] or None
            key = " ".join(stmt.lower().split())
            if key in seen:
                continue
            seen.add(key)
            ev = parse_event_date(stmt, date)     # #1: absolute event date
            if subj and pred:                     # belief-change: close out prior value for (subject,predicate)
                n_super += st.supersede_predicate(schema, ns, subj, pred, obj, ev)
            fid = st.insert_semantic(schema, ns, "fact", stmt, subject=(subj[:100] if subj else None),
                                     predicate=pred, obj=obj, valid_from=ev, salience=0.5, vec=embed_fn(stmt))
            # populate the ENTITY GRAPH from the SPO triple (every fact is an edge)
            if subj:
                se = st.upsert_entity(schema, ns, subj[:100])
                st.add_mention(schema, se, fid, "semantic")
                if obj:
                    oe = st.upsert_entity(schema, ns, obj[:100])
                    st.add_mention(schema, oe, fid, "semantic")
                    st.bump_edge(schema, ns, se, oe)
            ents = set(_PROPER.findall(stmt))    # full entity coverage for rich dossiers
            if subj:
                ents.add(subj)
            for e in ents:
                ent_facts[e].append((ev, stmt))

    # --- capped entity dossiers (offline multi-hop pre-join) ---
    clusters = sorted(((e, fs) for e, fs in ent_facts.items() if len(fs) >= min_facts),
                      key=lambda x: -len(x[1]))[:max_entities]
    def dossier(item):
        e, fs = item
        content = f"Subject: {e}\nKnown facts (dated):\n- " + "\n- ".join(f for _, f in fs[:40])
        vf = max((d for d, _ in fs), default=None)
        return e, vf, _facts(cli, model, DOSSIER_SYS, content, meter, "extract")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        dres = list(ex.map(dossier, clusters))
    embed_fn.prime([f for _, _, fs in dres for f in fs[:max_dossier]])
    n_dos = 0
    for e, vf, facts in dres:
        for f in facts[:max_dossier]:
            key = " ".join(f.lower().split())
            if key in seen:
                continue
            seen.add(key)
            vec = embed_fn(f)
            st.supersede_subject(schema, ns, e[:100], vec, vf)   # belief-change: close out prior value
            st.insert_semantic(schema, ns, "dossier", f, subject=e[:100], valid_from=vf, salience=0.8, vec=vec)
            n_dos += 1
    return {"facts": len(seen), "dossiers": n_dos, "superseded": n_super}


def retrieve(st, schema, ns, query, embed_fn, rerank_model, rlock, k=40,
             raw_quota=11, sem_quota=8, graph=False):
    """QUOTA retrieval (raw turns ⊕ semantic facts) with BI-TEMPORAL + TIMELINE awareness.
    For temporal questions, GUARANTEE the entity timeline (facts about the query's
    entities, sorted by event time / range-filtered) in the context — because vector
    search structurally misses dated evidence ('when did X' ≁ 'I moved out'). No LLM."""
    from datetime import datetime, timezone
    from memnos_brain import temporal as T
    now = st.max_observed_at(schema, ns) or datetime.now(timezone.utc)
    intent = T.analyze(query, now)
    qv = embed_fn(query)
    raw = st.search_raw_turns(schema, ns, qv, query, k)

    def rr(items, kind):
        if not items:
            return []
        with rlock:
            order = brain_rerank.rerank(query, [c["content"] for c in items], rerank_model)
        out = []
        for i, _ in order:
            row = {"content": items[i]["content"], "kind": kind}
            if kind == "fact" and items[i].get("valid_from"):
                row["date"] = items[i]["valid_from"].date().isoformat()
            out.append(row)
        return out

    if not intent.temporal:
        sem = st.search_semantic(schema, ns, qv, query, k)
        sem_rows = rr(sem, "fact")
        if not graph:
            return rr(raw, "turn")[:raw_quota] + sem_rows[:sem_quota]
        # GRAPH-TRAVERSAL arm: facts reachable from the query's entities (2-hop), guaranteed
        gx = st.graph_expand(schema, ns, T.query_entities(query), hops=2, limit=15)
        gseen, gx_rows = set(), []
        for r in gx:
            if r["content"] not in gseen:
                gseen.add(r["content"])
                gx_rows.append({"content": r["content"], "kind": "fact",
                                "date": r["valid_from"].date().isoformat() if r.get("valid_from") else None})
        sem_rows = [r for r in sem_rows if r["content"] not in gseen]
        return rr(raw, "turn")[:raw_quota] + gx_rows[:6] + sem_rows[:max(2, sem_quota - 6)]

    # --- TEMPORAL path: guaranteed timeline + reranked relevance facts ---
    ents = T.query_entities(query)
    tl = st.timeline(schema, ns, ents, start=intent.start, end=intent.end,
                     order=intent.order or "asc", limit=12)
    tl_rows, tl_seen = [], set()
    for r in tl:
        c = r["content"]
        if c in tl_seen:
            continue
        tl_seen.add(c)
        tl_rows.append({"content": c, "kind": "fact",
                        "date": r["valid_from"].date().isoformat() if r.get("valid_from") else None})
    sem = st.search_semantic_temporal(schema, ns, qv, query, k, start=intent.start,
                                      end=intent.end, current_only=intent.current, order=intent.order)
    sem_rows = [r for r in rr(sem, "fact") if r["content"] not in tl_seen]
    # raw (small) + GUARANTEED timeline + a few reranked relevance facts
    return rr(raw, "turn")[:5] + tl_rows[:12] + sem_rows[:6]


def fmt_context(rows, max_chars=9000):
    out, used = [], 0
    for r in rows:
        if r["kind"] == "fact":
            d = f", {r['date']}" if r.get("date") else ""
            line = f"- (fact{d}) {r['content']}"
        else:
            line = f"- (said) {r['content']}"
        if used + len(line) > max_chars:
            break
        out.append(line); used += len(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3,4")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--reranker", default="BAAI/bge-reranker-base")
    ap.add_argument("--k", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=16)
    ap.add_argument("--max-chars", type=int, default=9000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--graph", action="store_true", help="enable graph-traversal arm (recursive CTE)")
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    cli = OpenAI(max_retries=5)
    meter = TSCostMeter(budget_usd=args.budget)
    embed_fn = CachedEmbedder(cli, meter)
    rlock = threading.Lock()
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
            t0 = time.perf_counter()
            ing = ingest_sample(st, schema, ns, sample, cli, args.model, meter, embed_fn, args.workers)
            print(f"[{idx}] ingested {ing} in {time.perf_counter()-t0:.0f}s ({meter.summary()})", flush=True)

            pool = Queue()
            for _ in range(args.workers):
                pool.put(BrainStore(DSN))
            def do_qa(q):
                question, expected = q.get("question", ""), str(q.get("answer", ""))
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                if not question or not expected:
                    return None
                s2 = pool.get()
                try:
                    rows = retrieve(s2, schema, ns, question, embed_fn, args.reranker, rlock,
                                    k=args.k, graph=args.graph)
                finally:
                    pool.put(s2)
                ctx = fmt_context(rows, max_chars=args.max_chars)   # surfaces fact dates
                pred = answer_from_ctx(cli, args.model, question, ctx, meter, relaxed=True)
                return cat, judge(cli, args.judge_model, question, expected, pred, meter)
            qa = sample.get("qa", [])
            t1 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for r in ex.map(do_qa, qa):
                    if r:
                        results.append(r)
            while not pool.empty():
                pool.get().conn.close()
            print(f"[{idx}] answered {len(qa)} QA in {time.perf_counter()-t1:.0f}s ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results:
        cats.setdefault(cat, []).append(sc)
    print("\n=== PHASE A (recombination) — LoCoMo ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls:
        print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
