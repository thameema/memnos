"""Temporal failure diagnostic — is the temporal score (12%) lost at RETRIEVAL or
at ANSWER/date-reasoning? For each LoCoMo temporal question over the Phase A schemas:
  - evidence_recall: is the supporting turn (LoCoMo `evidence` dia_id) retrieved?
  - answer_in_context: does the gold answer string appear in the retrieved context?
High recall + low answer = answer/date-reasoning problem. Low recall = retrieval.
Pure embeddings (free)."""
import sys
sys.path.insert(0, ".")
import threading
import httpx
from openai import OpenAI
from memnos_brain import BrainStore
from phaseA_recombine import retrieve, fmt_context
from validate_brain import CachedEmbedder
from locomo_pg_parallel import TSCostMeter

URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def norm(s):
    return " ".join(str(s).lower().split())


def main():
    data = httpx.get(URL, follow_redirects=True, timeout=60).json()
    cli = OpenAI(max_retries=5)
    embed = CachedEmbedder(cli, TSCostMeter())
    rlock = threading.Lock()
    st = BrainStore("postgresql://memnos:memnos_poc@localhost:5433/memnos")

    n = ev_hit = ans_hit = both = 0
    for idx in (2, 3, 4):
        sample = data[idx]; sid = str(sample.get("sample_id", idx))
        schema = f"tenant_pa{idx}"; ns = f"locomo:{sid}"
        conv = sample.get("conversation", {})
        dmap = {}
        for k, v in conv.items():
            if k.startswith("session_") and isinstance(v, list):
                for t in v:
                    if isinstance(t, dict) and t.get("dia_id"):
                        dmap[t["dia_id"]] = t.get("text", "")
        for q in sample.get("qa", []):
            if q.get("category") != 3:        # temporal only
                continue
            question, expected = q.get("question", ""), norm(q.get("answer", ""))
            ev = [norm(dmap.get(e, "")) for e in (q.get("evidence") or []) if isinstance(e, str) and dmap.get(e)]
            ev = [e for e in ev if e]
            if not question or not expected or not ev:
                continue
            n += 1
            rows = retrieve(st, schema, ns, question, embed, "BAAI/bge-reranker-base", rlock, k=40)
            ctx = norm(fmt_context(rows, max_chars=12000))
            e_hit = any(e in ctx for e in ev)
            a_hit = expected in ctx if len(expected) > 2 else False
            ev_hit += e_hit; ans_hit += a_hit; both += (e_hit and a_hit)

    print(f"\n=== TEMPORAL diagnostic (n={n} questions w/ evidence) ===")
    print(f"  evidence retrieved (recall):   {ev_hit}/{n} = {100*ev_hit/n:.0f}%")
    print(f"  gold answer present in context: {ans_hit}/{n} = {100*ans_hit/n:.0f}%")
    print(f"  both:                          {both}/{n} = {100*both/n:.0f}%")
    print("\nREAD: high evidence-recall + low answer-accuracy (12%) => ANSWER/date-reasoning problem.")
    print("      low evidence-recall => RETRIEVAL problem (wrong/missing dated fact).")


if __name__ == "__main__":
    main()
