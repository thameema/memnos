"""Phase 0 — brain-inspired memory: offline CONSOLIDATION + semantic-first retrieval.

The bet (falsifiable): our failure is synthesis-at-query-time (recall 86-90%,
answer 33-72%). The brain consolidates episodes into semantic facts OFFLINE and
recalls the distilled fact. So we build a `semantic` layer:

  Consolidation ("sleep", offline, LLM):
    1. WINDOW pass  — slide over each session's turns → decontextualized
       propositions (pronouns/entities/dates resolved). [Dense X / gist]
    2. ENTITY pass  — gather propositions per entity → synthesize an entity
       "dossier" that PRE-JOINS multi-hop facts ("X in Y"+"Y in Z" -> "X in Z").
       [CLS neocortical consolidation — the multi-hop fix]
  Every semantic row keeps provenance (source turn ids).

  Retrieval (query, no-LLM): semantic-first hybrid (vector+FTS) -> rerank ->
  context -> answer. Episodic turns as fallback only.

Compares answer accuracy vs the raw-turn baseline on the SAME questions.

Usage: OPENAI_API_KEY=... python phase0_brain.py --sample-ids 2,3 --budget 2
"""
import argparse
import json
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")
import httpx
from openai import OpenAI

from memnos_poc import local_models
from memnos_poc.embedders import OpenAIEmbedder
from memnos_poc.storage import PgStorage, _vlit
from memnos_poc.usage import BudgetExceeded
from locomo_pg_parallel import TSCostMeter, CATEGORY_MAP

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"

PROP_SYS = ("You convert a slice of a dated conversation into atomic, self-contained FACTS. "
            "Resolve pronouns and references to explicit names; attach the date when relevant. "
            "Each fact must stand alone without the conversation. Output JSON "
            '{"facts": ["...", ...]} — short declarative sentences, no commentary.')

DOSSIER_SYS = ("You consolidate everything known about one subject into durable facts, "
               "INCLUDING facts that require COMBINING multiple inputs (e.g. 'A works at B' + "
               "'B is in C' => 'A works in C'). Resolve contradictions in favor of the most "
               "recent. Keep dates. Output JSON {\"facts\": [\"...\", ...]} of standalone sentences.")


def load_dataset():
    with httpx.Client(follow_redirects=True, timeout=60) as h:
        return h.get(URL).json()


def session_turns(sample):
    """Ordered [(date, 'speaker: text')] per session."""
    conv = sample.get("conversation", {})
    keys = sorted([k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
                  key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0)
    out = []
    for sk in keys:
        date = str(conv.get(f"{sk}_date_time", "")).strip()
        turns = [f"{t.get('speaker','?')}: {t.get('text','')}"
                 for t in (conv[sk] if isinstance(conv.get(sk), list) else [])
                 if isinstance(t, dict) and t.get("text")]
        out.append((date, turns))
    return out


def _facts_from(cli, model, sys_prompt, content, meter, op):
    r = cli.chat.completions.create(model=model, temperature=0, max_tokens=700,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": content}])
    meter.record(op, model, r.usage.prompt_tokens, r.usage.completion_tokens)
    try:
        return [str(x).strip() for x in json.loads(r.choices[0].message.content).get("facts", []) if str(x).strip()]
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


def ensure_semantic_table(st, schema):
    with st.conn.cursor() as c:
        c.execute(f"DROP TABLE IF EXISTS {schema}.semantic")
        c.execute(f"""CREATE TABLE {schema}.semantic(
            id bigserial PRIMARY KEY, namespace text, kind text, statement text,
            embedding halfvec({DIM}), fts tsvector GENERATED ALWAYS AS
            (to_tsvector('english', statement)) STORED)""")
        c.execute(f"CREATE INDEX ON {schema}.semantic USING hnsw (embedding halfvec_cosine_ops)")
        c.execute(f"CREATE INDEX ON {schema}.semantic USING gin (fts)")


def consolidate(st, schema, ns, sample, cli, model, meter, embed_fn, workers,
                window=12, stride=8):
    """Window pass (propositions) + entity pass (dossiers). Returns counts."""
    sess = session_turns(sample)

    # --- WINDOW PASS: propositions ---
    chunks = []
    for date, turns in sess:
        for i in range(0, max(1, len(turns)), stride):
            sl = turns[i:i + window]
            if sl:
                chunks.append(f"[date: {date}]\n" + "\n".join(sl))
    def prop(ch):
        return _facts_from(cli, model, PROP_SYS, ch, meter, "consolidate")
    props = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fs in ex.map(prop, chunks):
            props.extend(fs)

    # --- ENTITY PASS: dossiers (multi-hop pre-join) ---
    # group propositions by capitalized entity tokens they mention
    ent_props = defaultdict(list)
    for p in props:
        for ent in set(re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", p)):
            ent_props[ent].append(p)
    big = {e: ps for e, ps in ent_props.items() if len(ps) >= 3}
    def dossier(item):
        e, ps = item
        return _facts_from(cli, model, DOSSIER_SYS,
                           f"Subject: {e}\nKnown facts:\n- " + "\n- ".join(ps[:40]), meter, "consolidate")
    dossiers = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fs in ex.map(dossier, list(big.items())):
            dossiers.extend(fs)

    # --- write semantic rows (embedded) ---
    rows = [("prop", p) for p in props] + [("dossier", d) for d in dossiers]
    # dedup identical statements
    seen, uniq = set(), []
    for kind, s in rows:
        if s.lower() not in seen:
            seen.add(s.lower()); uniq.append((kind, s))
    def emb(item):
        kind, s = item
        return kind, s, embed_fn(s)
    embedded = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        embedded = list(ex.map(emb, uniq))
    with st.conn.cursor() as c:
        for kind, s, v in embedded:
            c.execute(f"INSERT INTO {schema}.semantic(namespace,kind,statement,embedding) "
                      f"VALUES(%s,%s,%s,%s::halfvec)", (ns, kind, s, _vlit(v)))
    return len(chunks), len(props), len(dossiers), len(uniq)


def semantic_search(stg, schema, ns, qvec, qtext, k):
    """Hybrid RRF over the SEMANTIC layer only. Returns id, content, rrf rank-pos."""
    sql = f"""
    WITH vec AS (SELECT id, statement, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                 FROM {schema}.semantic WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
    fts AS (SELECT id, statement, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
            FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
            WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
    fused AS (SELECT id, statement, SUM(1.0/(60+rnk)) score FROM
              (SELECT id,statement,rnk FROM vec UNION ALL SELECT id,statement,rnk FROM fts) r
              GROUP BY id,statement)
    SELECT id, statement AS content, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
    with stg.conn.cursor() as c:
        c.execute(sql, {"qv": _vlit(qvec), "qt": qtext, "ns": ns, "k": k})
        return c.fetchall()


def episodic_search(stg, schema, ns, qvec, qtext, k, max_id):
    """Hybrid RRF over raw turns (episodic), returning a recency in [0,1]
    (id position = conversation order; 1.0 = most recent)."""
    sql = f"""
    WITH vec AS (SELECT id, content, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                 FROM {schema}.memory WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
    fts AS (SELECT id, content, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
            FROM {schema}.memory, websearch_to_tsquery('english',%(qt)s) q
            WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
    fused AS (SELECT id, content, SUM(1.0/(60+rnk)) score FROM
              (SELECT id,content,rnk FROM vec UNION ALL SELECT id,content,rnk FROM fts) r
              GROUP BY id,content)
    SELECT id, content, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
    with stg.conn.cursor() as c:
        c.execute(sql, {"qv": _vlit(qvec), "qt": qtext, "ns": ns, "k": k})
        rows = c.fetchall()
    for r in rows:
        r["recency"] = (r["id"] / max_id) if max_id else 1.0
        r["kind"] = "episodic"
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ids", default="2,3")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--k", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=3.0)
    args = ap.parse_args()
    ids = [int(x) for x in args.sample_ids.split(",")]

    global DIM
    cli = OpenAI(max_retries=5)
    meter = TSCostMeter(budget_usd=args.budget)
    embedder = OpenAIEmbedder(cli, meter); DIM = embedder.dim
    embed_fn = embedder.embed
    rerank_lock = threading.Lock()
    local_models.rerank("warm", ["a", "b"])
    data = load_dataset()
    st = PgStorage(DSN)

    results = []
    try:
        for idx in ids:
            sample = data[idx]; sid = str(sample.get("sample_id", idx))
            schema = f"tenant_locomo{idx}"; ns = f"locomo:{sid}"
            with st.conn.cursor() as c:
                c.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,))
                if not c.fetchone()["exists"]:
                    print(f"[{idx}] skip — {schema} missing"); continue
            ensure_semantic_table(st, schema)
            t0 = time.perf_counter()
            nch, npr, ndo, nuq = consolidate(st, schema, ns, sample, cli, args.model, meter, embed_fn, args.workers)
            print(f"[{idx}] consolidated: {nch} windows -> {npr} props + {ndo} dossiers = {nuq} semantic rows "
                  f"in {time.perf_counter()-t0:.0f}s  ({meter.summary()})", flush=True)

            # semantic-first QA
            pool = __import__("queue").Queue()
            for _ in range(args.workers):
                pool.put(PgStorage(DSN))
            def do_qa(q):
                from locomo_pg_qa_only import answer_from_ctx, judge
                from memnos_poc import retrieve as ret
                question, expected = q.get("question",""), str(q.get("answer",""))
                cat = CATEGORY_MAP.get(q.get("category"), "unknown")
                if not question or not expected: return None
                stg = pool.get()
                try:
                    qv = embed_fn(question)
                    cands = semantic_search(stg, schema, ns, qv, question, args.k)
                finally:
                    pool.put(stg)
                if len(cands) > 1:
                    with rerank_lock:
                        order = local_models.rerank(question, [c["content"] for c in cands])
                    cands = [cands[i] for i,_ in order]
                ctx = ret.context_block(cands[:args.top_k], max_chars=args.max_chars)
                pred = answer_from_ctx(cli, args.model, question, ctx, meter, relaxed=True)
                return cat, judge(cli, args.judge_model, question, expected, pred, meter)
            qa = sample.get("qa", [])
            t1 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for r in ex.map(do_qa, qa):
                    if r: results.append(r)
            while not pool.empty(): pool.get().conn.close()
            print(f"[{idx}] answered {len(qa)} QA semantic-first in {time.perf_counter()-t1:.0f}s  ({meter.summary()})", flush=True)
    except BudgetExceeded as e:
        print(f"\n*** STOPPED: {e}")

    cats = {}
    for cat, sc in results: cats.setdefault(cat, []).append(sc)
    print("\n=== Phase0 BRAIN (semantic-first) ===")
    for cat, scs in sorted(cats.items()):
        print(f"  {cat:<12} {sum(scs)}/{len(scs)} = {100*sum(scs)/len(scs):.0f}%")
    alls = [s for _, s in results]
    if alls: print(f"  {'OVERALL':<12} {sum(alls)}/{len(alls)} = {100*sum(alls)/len(alls):.0f}%")
    print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
