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


# --- adaptive, self-calibrating cap (follow-up to #12) -------------------------------
# Field failure on a CPU-class laptop: a FIXED cap=100 × ~70-90ms/pair on CPU was the
# ENTIRE warm latency (rerank_ms was 85% of every call). The cap must instead be DERIVED
# from a per-install latency budget against the measured ms-per-pair of THIS box:
#   effective_cap = clamp(floor(BUDGET_MS / measured_ms_per_pair), MIN_CAP, MAX_CAP)
# An explicit MEMNOS_RERANK_CAP always WINS (operator override). Until calibration runs
# (background prewarm), the conservative MIN_CAP is used so a cold box never blows budget.
#
# BUDGET is a CEILING ("don't let warm recall exceed ~1.5s"), NOT an aggressive target.
# At 1500ms the budget keeps CAPABLE hardware (low ms/pair) clamped at the full MAX_CAP=100
# — i.e. ranking is IDENTICAL to the already-LoCoMo-gated cap=100 config that scored 65%,
# so no new (paid) LoCoMo run is needed to ship safely (accuracy parity). ONLY slow hardware
# degrades: e.g. ~80ms/pair → 1500/80 ≈ 18 → ~1.4s, usable, vs the old 8-50s timeout. This
# is the single place behavior changes, and it changes for the better. Owner directive: a
# strong LoCoMo score matters more than reranker size/latency, so capable HW must hold cap=100.
_BUDGET_MS_DEFAULT = 1500.0
_MIN_CAP_DEFAULT = 8
_MAX_CAP_DEFAULT = 100

# calibration state (set by background prewarm); _ready gates degraded-while-warming
_calib_lock = threading.Lock()
_measured_ms_per_pair: float | None = None   # None until prewarm calibrates
_effective_cap: int | None = None
_ready = threading.Event()                    # set when the model is loaded + calibrated


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _explicit_cap() -> int | None:
    """Operator override — MEMNOS_RERANK_CAP, if set & parseable. Wins over derivation."""
    raw = os.environ.get("MEMNOS_RERANK_CAP")
    if raw is None or raw.strip() == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _simulated_ms_per_pair() -> float | None:
    """Test hook: force a per-pair latency so a fast runner can simulate a CPU-class box
    deterministically (gate requirement). None = measure for real."""
    raw = os.environ.get("MEMNOS_RERANK_SIMULATED_MS_PER_PAIR")
    if raw is None or raw.strip() == "":
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def derive_cap(ms_per_pair: float, *, budget_ms: float | None = None,
               min_cap: int | None = None, max_cap: int | None = None) -> int:
    """effective_cap = clamp(floor(budget / ms_per_pair), MIN_CAP, MAX_CAP)."""
    budget_ms = _env_float("MEMNOS_RERANK_BUDGET_MS", _BUDGET_MS_DEFAULT) if budget_ms is None else budget_ms
    min_cap = _env_int("MEMNOS_RERANK_MIN_CAP", _MIN_CAP_DEFAULT) if min_cap is None else min_cap
    max_cap = _env_int("MEMNOS_RERANK_MAX_CAP", _MAX_CAP_DEFAULT) if max_cap is None else max_cap
    if ms_per_pair <= 0:
        return max_cap
    return max(min_cap, min(max_cap, int(budget_ms // ms_per_pair)))


def _cap() -> int:
    """Effective cap fed to the cross-encoder. Precedence:
      1. explicit MEMNOS_RERANK_CAP (operator override, incl. 0 = uncapped)
      2. derived cap from the calibrated ms-per-pair (set by background prewarm)
      3. MIN_CAP fallback before calibration completes (conservative — never blows budget)"""
    explicit = _explicit_cap()
    if explicit is not None:
        return explicit
    with _calib_lock:
        if _effective_cap is not None:
            return _effective_cap
    return _env_int("MEMNOS_RERANK_MIN_CAP", _MIN_CAP_DEFAULT)


def is_ready() -> bool:
    """True once the model is loaded + calibrated. Recalls arriving before this serve
    degraded (RRF order, degraded:true) instead of blocking behind the heavy load.
    MEMNOS_RERANK_SIMULATE_WARMING=1 forces False (test hook: a runner can assert the
    degraded-while-warming path without racing the real background load)."""
    if os.environ.get("MEMNOS_RERANK_SIMULATE_WARMING", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    return (not _enabled()) or _ready.is_set()


def calibration() -> dict:
    """Snapshot for the audit ledger / operator log: what this box calibrated to."""
    with _calib_lock:
        mpp = _measured_ms_per_pair
    # _cap() acquires _calib_lock too — call it OUTSIDE the with (Lock is non-reentrant)
    return {"ready": _ready.is_set(),
            "measured_ms_per_pair": round(mpp, 2) if mpp is not None else None,
            "effective_cap": _cap()}


def _reset_calibration_for_tests():
    """Test-only: clear calibration state so a fresh prewarm can recalibrate."""
    global _measured_ms_per_pair, _effective_cap
    with _calib_lock:
        _measured_ms_per_pair = None
        _effective_cap = None
    _ready.clear()


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


# --- memory bounding (issue #15) -----------------------------------------------------
# The ONNX Runtime CPU memory ARENA is the dominant retained-memory contributor on the
# recall path: it grabs a high-water-mark block on the first inference (sized for the
# largest batch the cross-encoder ever sees — cap candidates × the model's truncation
# length) and NEVER returns it to the OS, even at idle. Identical trivial inputs drive it
# (input-independent), it plateaus, and it never recedes — exactly the #15 symptom. With
# the default 80MB MiniLM this is a few hundred MB; with a 1GB model (bge-reranker-base)
# or a 16GB host it is the multi-GB plateau / swap blow-up reported.
#
# Fix: build the InferenceSession with the arena DISABLED so allocations are released back
# to the OS after each forward pass, and pin intra/inter-op threads to 1 (each worker
# thread otherwise reserves its own arena slab — N× the footprint on a many-core box).
# fastembed exposes `enable_cpu_mem_arena` (EXPOSED_SESSION_OPTIONS) and `threads`
# (→ intra_op_num_threads + inter_op_num_threads) through TextCrossEncoder kwargs; the
# tokenizer is already truncation-bounded at the model's max_length by fastembed, so the
# per-pair allocation is fixed. This does NOT change the math — same weights, same logits,
# same ranking (verified by a seeded fixed-input score-parity check; arena/threads affect
# allocation + scheduling only, not the computation).
#
# Escape hatches (operator-tunable, defaults = the bounded behavior):
#   MEMNOS_RERANK_ARENA=1   re-enable the arena (revert to the old unbounded behavior)
#   MEMNOS_RERANK_THREADS=N session intra/inter-op threads (default 1; 0 = library default)
def _arena_enabled() -> bool:
    return os.environ.get("MEMNOS_RERANK_ARENA", "0").strip().lower() in ("1", "true", "yes", "on")


def _rerank_threads() -> int | None:
    """Session thread count (default 1 — one arena slab, not one per core). 0 = library default."""
    n = _env_int("MEMNOS_RERANK_THREADS", 1)
    return None if n <= 0 else n


def _model_cache_dir() -> str:
    """Persistent model cache under ~/.memnos/models — survives reboots and macOS TMPDIR
    purges (the default cache is TMPDIR which macOS clears at reboot, causing recall 500s
    after every restart when the ONNX blob is gone but the symlink remains)."""
    d = os.path.join(os.path.expanduser("~"), ".memnos", "models")
    os.makedirs(d, exist_ok=True)
    return d


@functools.lru_cache(maxsize=2)
def _model(name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=name,
                            threads=_rerank_threads(),
                            enable_cpu_mem_arena=_arena_enabled(),
                            cache_dir=_model_cache_dir())


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
    sim = _simulated_ms_per_pair()
    if sim is not None:                                      # test hook: emulate a slow box
        time.sleep(sim * len(head) / 1000.0)
    try:
        logits = list(_model(model).rerank(query, head))     # raw logits, candidate order
    except Exception as e:
        # Model file missing/corrupt (e.g. macOS TMPDIR purge with stale symlink).
        # Evict the broken cached model object, serve this request from RRF order,
        # and kick off a background re-download so the next request can score properly.
        _model.cache_clear()
        print(f"[memnos] WARN: reranker load failed ({e}); degrading to RRF, re-downloading in background", flush=True)

        def _redownload():
            try:
                _model(model)           # blocks until download complete; re-populates lru_cache
                print(f"[memnos] reranker re-downloaded to {_model_cache_dir()}", flush=True)
            except Exception as e2:
                print(f"[memnos] WARN: reranker re-download failed: {e2}", flush=True)

        threading.Thread(target=_redownload, name="memnos-rerank-redownload", daemon=True).start()
        return _passthrough(n)
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


def _measure_ms_per_pair(model: str, n: int) -> float:
    """Time a real (or simulated) rerank batch on THIS hardware → ms-per-pair. A
    simulated value (test hook) short-circuits the timing so any runner can emulate a
    CPU-class box deterministically."""
    sim = _simulated_ms_per_pair()
    if sim is not None:
        return sim
    cands = _prewarm_candidates(n)
    t0 = time.perf_counter()
    list(_model(model).rerank(_PREWARM_QUERY, cands))    # bypass cap: time the raw batch
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return dt_ms / max(1, len(cands))


def prewarm(model: str = DEFAULT_RERANKER, n: int = 32) -> float:
    """Boot prewarm with a REALISTIC batch (default 32 candidates of realistic length):
    loads the model, grows the ONNX arena, JITs the real code paths, AND self-calibrates
    the effective rerank cap to THIS install's hardware (follow-up to #12): it measures
    ms-per-pair here and derives effective_cap = clamp(BUDGET/ms_per_pair, MIN, MAX) so a
    CPU-class box gets a small cap and a fast box a large one — instead of the fixed
    cap=100 that made rerank 85% of every warm call on a laptop. Sets the ready flag so
    recalls before this point serve degraded (RRF order) rather than blocking on the load.
    No-op when MEMNOS_RERANK=0. Returns elapsed ms (logged by the server)."""
    global _measured_ms_per_pair, _effective_cap
    if not _enabled():
        _ready.set()
        return 0.0
    t0 = time.perf_counter()
    mpp = _measure_ms_per_pair(model, n)
    rerank(_PREWARM_QUERY, _prewarm_candidates(n), model)   # warm the real (capped) path
    cap = derive_cap(mpp)
    with _calib_lock:
        _measured_ms_per_pair = mpp
        _effective_cap = cap
    _ready.set()
    eff = _explicit_cap()
    print(f"[memnos] rerank calibrated: measured_ms_per_pair={mpp:.2f} "
          f"derived_cap={cap}" + (f" (overridden by MEMNOS_RERANK_CAP={eff})"
                                  if eff is not None else ""), flush=True)
    return (time.perf_counter() - t0) * 1000.0


def prewarm_background(model: str = DEFAULT_RERANKER, n: int = 32):
    """Run prewarm() off the request path so the server accepts traffic IMMEDIATELY on
    boot (follow-up to #12: the synchronous prewarm cost 13-114s at startup and recalls
    blocked behind it). Until the flag is set, is_ready() is False and recalls serve
    degraded. Returns the daemon thread (or None when disabled)."""
    if not _enabled():
        _ready.set()
        return None

    def _run():
        try:
            ms = prewarm(model, n)
            if ms:
                print(f"[memnos] reranker prewarmed in {ms:.0f}ms ({model})", flush=True)
        except Exception as e:                  # never take the server down on a warm failure
            print(f"[memnos] WARN: reranker prewarm failed: {e}", flush=True)
            _ready.set()                         # unblock — recalls fall back to RRF order

    t = threading.Thread(target=_run, name="memnos-rerank-prewarm", daemon=True)
    t.start()
    return t


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
