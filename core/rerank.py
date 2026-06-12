"""Configurable cross-encoder reranker (B3).

The reranker is the biggest single retrieval-quality lever, and it is NOT a generative LLM,
so the 'no LLM at query time' moat is preserved. Runs on ONNX Runtime via `fastembed` —
no torch — so the install stays light. Lazily loaded + cached per model.

Output parity: `fastembed` returns the model's raw relevance logit; we apply a sigmoid so
scores match the previous sentence-transformers `CrossEncoder.predict()` output (0-1) exactly
— a drop-in for everything downstream.
"""
from __future__ import annotations

import functools
import math
import os
import threading
import time

# Default reranker. Override per deployment via MEMNOS_RERANKER (e.g.
# BAAI/bge-reranker-large for max accuracy, or Xenova/ms-marco-MiniLM-L-6-v2 for a
# small-RAM host: bge-reranker-base is a 1.0 GB fp32 ONNX whose working set is ~1.9 GB
# resident while hot — the RSS "sawtooth" of issue #8 is this model paging in on recall
# bursts and being reclaimed by the OS at idle). Changing the model changes ranking
# quality — re-run the LoCoMo benchmark before switching a production deployment.
# Default chosen by measured LoCoMo A/B on the IDENTICAL full-10 corpus (2026-06-10,
# n=1542, same answer+judge): MiniLM-L-6 65% vs bge-reranker-base 59% — the 80MB model
# BEAT the 1.04GB one by +6pp while being 8.4x faster (118ms vs 986ms / 80 candidates),
# ~660MB lighter resident, 0.23s vs 1.5s cold start. Override: MEMNOS_RERANKER.
DEFAULT_RERANKER = os.environ.get("MEMNOS_RERANKER", "Xenova/ms-marco-MiniLM-L-6-v2")

# Latency knobs (issue #12). Read at CALL time (cheap getenv) so tests/operators can
# flip them without a restart of the importing process:
#   MEMNOS_RERANK=0          kill switch — recall falls back to retrieval (RRF) order,
#                            no cross-encoder, model never loaded (parity with the
#                            negation/dedupe/broad-tune zero-disable switches)
#   MEMNOS_RERANK_CAP=N      cap the candidates FED to the cross-encoder (default 100 —
#                            chosen ABOVE the engine's worst per-arm candidate count at
#                            canonical k=40: the temporal semantic arm can return up to
#                            k + k + 6 = 86 rows, so 100 is a no-op for the benchmarked
#                            LoCoMo config and only bounds pathological fan-outs
#                            [grounded/wide merges]; LoCoMo full-10 gate re-run at this
#                            default: 64%, identical to the issue-11 baseline). 0 = uncapped.
#                            Beyond-cap rows are NOT dropped — they keep their retrieval
#                            (RRF) order, scored strictly below the reranked minimum.


def _enabled() -> bool:
    return os.environ.get("MEMNOS_RERANK", "1").strip().lower() not in ("0", "false", "no", "off")


def _cap() -> int:
    try:
        return max(0, int(os.environ.get("MEMNOS_RERANK_CAP", "100")))
    except (TypeError, ValueError):
        return 100


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@functools.lru_cache(maxsize=2)
def _model(name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=name)


def _passthrough(n: int) -> list[tuple[int, float]]:
    """Retrieval-order fallback: candidates arrive ranked by RRF score, so identity
    order with a strictly-decreasing positive score is the best no-model ranking."""
    return [(i, 1.0 / (1.0 + i)) for i in range(n)]


def rerank(query: str, candidates: list[str], model: str = DEFAULT_RERANKER) -> list[tuple[int, float]]:
    """Return [(orig_index, score)] sorted best-first. Score = sigmoid(relevance logit), 0-1.
    MEMNOS_RERANK=0 → retrieval-order passthrough (model never loaded);
    MEMNOS_RERANK_CAP bounds the cross-encoder batch (see knob notes above)."""
    if not candidates:
        return []
    n = len(candidates)
    if not _enabled():
        return _passthrough(n)
    cap = _cap()
    head = candidates if (cap == 0 or n <= cap) else candidates[:cap]
    logits = list(_model(model).rerank(query, head))         # raw logits, candidate order
    scores = [_sigmoid(float(s)) for s in logits]
    out = sorted(((i, scores[i]) for i in range(len(head))), key=lambda x: x[1], reverse=True)
    if len(head) < n:
        # beyond-cap tail: kept (never dropped), retrieval order, strictly below the
        # reranked minimum so capped rows can't leapfrog a cross-encoder-scored one
        floor = min(scores) if scores else 1.0
        tail_n = n - len(head)
        out += [(len(head) + j, floor * (tail_n - j) / (tail_n + 1.0)) for j in range(tail_n)]
    return out


# --- residency (issue #12: cold-start spikes + macOS page-out) -----------------------
# Field evidence (45K-fact namespace): the token-sized boot warm ('warm', ['a','b'])
# left the first REAL call paying 6-13s of lazy-init/JIT/arena growth, and after hours
# of idle macOS paged the ONNX weights out — the next recall stalled ~20s paging the
# model back in (client MCP timeout). Two fixes: a REALISTIC boot prewarm (real model,
# realistic candidate count + lengths) and a periodic keep-alive inference whose memory
# touch defeats the pager.

_PREWARM_QUERY = "where do things stand with the alpha deployment and what changed since last week?"
_PREWARM_SEED = [
    "The {p} pipeline was switched to the new ingestion backend on {d} after the capacity "
    "review; throughput is now around {n}00 requests per second under the revised limits.",
    "{who} said the {p} migration is no longer blocked by the schema rollout and the team "
    "plans to finish the cutover by the end of the sprint, pending the security sign-off.",
    "On {d} the on-call rotation flagged that the {p} service hit its memory threshold "
    "twice; the recommended action is to raise the worker cap from {n} to {n}2 instances.",
    "{who} moved the quarterly planning session to {d} and asked everyone to bring the "
    "latency numbers for the {p} cluster, including the p95 figures from the audit ledger.",
]


def _prewarm_candidates(n: int = 32) -> list[str]:
    """n realistic-length (~180-260 char) synthetic memory rows — the shape a real
    recall feeds the cross-encoder, unlike the old two-token warm."""
    people = ("Ada", "Bob", "Carol", "Deepak", "Elena", "Farid", "Grace", "Hugo")
    projects = ("alpha", "billing", "ingest", "gateway", "search", "ledger", "deploy", "auth")
    out = []
    for i in range(n):
        t = _PREWARM_SEED[i % len(_PREWARM_SEED)]
        out.append(t.format(p=projects[i % len(projects)], who=people[i % len(people)],
                            d=f"2026-0{1 + i % 9}-{10 + i % 18}", n=2 + i % 7))
    return out


def prewarm(model: str = DEFAULT_RERANKER, n: int = 32) -> float:
    """Boot prewarm with a REALISTIC batch (default 32 candidates of realistic length):
    loads the model, grows the ONNX arena, and JITs the real code paths so the first
    field call costs ~one warm inference, not 6-13s. No-op when MEMNOS_RERANK=0.
    Returns elapsed ms (logged by the server)."""
    if not _enabled():
        return 0.0
    t0 = time.perf_counter()
    rerank(_PREWARM_QUERY, _prewarm_candidates(n), model)
    return (time.perf_counter() - t0) * 1000.0


def start_keepalive(model: str = DEFAULT_RERANKER, interval_s: float | None = None):
    """Daemon keep-alive: a tiny rerank every MEMNOS_RERANK_KEEPALIVE_S seconds
    (default 240; 0 disables). One forward pass touches every weight page, so the OS
    never reclaims the resident model during idle — this is what killed the field
    deployment (~20s page-in after hours idle). Returns the thread (or None)."""
    if interval_s is None:
        try:
            interval_s = float(os.environ.get("MEMNOS_RERANK_KEEPALIVE_S", "240"))
        except (TypeError, ValueError):
            interval_s = 240.0
    if interval_s <= 0 or not _enabled():
        return None
    cands = _prewarm_candidates(4)

    def _loop():
        while True:
            time.sleep(interval_s)
            try:
                rerank(_PREWARM_QUERY, cands, model)
            except Exception:
                pass                       # keep-alive must never take the server down

    t = threading.Thread(target=_loop, name="memnos-rerank-keepalive", daemon=True)
    t.start()
    return t
