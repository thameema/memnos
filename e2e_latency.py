"""POC-1b: end-to-end latency = local query-embed + retrieve(POC-5) + local rerank,
plus a demo that the cross-encoder reorders results for precision.
"""
import sys
import time

sys.path.insert(0, ".")
from memnos_core import local_models as lm

RETRIEVE_P95_MS = 77.2  # measured in POC-5 (50k rows, hybrid one round-trip)

DOCS = [
    "The Acme merger is handled by the M&A team.",
    "Jane moved to the M&A team in March.",
    "Bob is Jane's paralegal.",
    "Jane used to be on the Litigation team.",
    "Acme's headquarters are in Chicago.",
    "The office coffee machine was repaired last week.",
    "The merger closing date was pushed to Q3.",
    "Acme retained outside counsel for the deal.",
]
QUERY = "who is working on the acme merger deal"


def main():
    # warm up (first call downloads/initializes the models)
    t = time.perf_counter(); lm.embed("warmup"); lm.rerank("warmup", ["a", "b"])
    print(f"model load+warmup: {time.perf_counter()-t:.1f}s\n")

    # 1) query embedding latency
    lat = []
    for _ in range(20):
        t = time.perf_counter(); lm.embed(QUERY); lat.append((time.perf_counter()-t)*1000)
    lat.sort(); embed_ms = lat[len(lat)//2]
    print(f"local query-embed (bge-small): p50 = {embed_ms:.1f} ms")

    # 2) rerank latency over 20 candidates
    cands20 = (DOCS * 3)[:20]
    lat = []
    for _ in range(20):
        t = time.perf_counter(); lm.rerank(QUERY, cands20); lat.append((time.perf_counter()-t)*1000)
    lat.sort(); rerank_ms = lat[len(lat)//2]
    print(f"local cross-encoder rerank (20 cands): p50 = {rerank_ms:.1f} ms\n")

    # 3) demo: rerank reorders for precision
    order = lm.rerank(QUERY, DOCS)
    print(f"QUERY: {QUERY!r}\nrerank top-4:")
    for rank, (i, score) in enumerate(order[:4], 1):
        print(f"  {rank}. ({score:+.2f})  {DOCS[i]}")

    # 4) end-to-end budget
    e2e = embed_ms + RETRIEVE_P95_MS + rerank_ms
    print(f"\nend-to-end (local embed + retrieve p95 {RETRIEVE_P95_MS}ms + rerank):")
    print(f"  ≈ {e2e:.0f} ms   →  {'PASS ✅' if e2e < 200 else 'FAIL ❌'} (<200ms budget)")


if __name__ == "__main__":
    main()
