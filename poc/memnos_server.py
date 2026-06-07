"""memnos production memory server — pooled, authenticated, namespace-ACL'd, audited.

Hardening vs the POC:
  - Bearer-token auth (server-side identity; never client-trusted) → principal
  - Namespace ACL: every read/write clamped to the principal's grants
  - Connection POOL (psycopg_pool) — no connection-per-request exhaustion
  - Input validation + size limits + structured errors (no stack-trace leaks)
  - Audit log (who/what/when) + usage ledger (cost) — the governance moat
  - /healthz (liveness) + /readyz (DB reachable)

Config via env: MEMNOS_DSN, MEMNOS_PORT, MEMNOS_POOL_MAX, OPENAI_API_KEY (enables
1536-d embeddings + extraction; else free local 384-d).
Bootstrap identity with: python memnos_admin.py ...
"""
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")


def _load_env(path=".env"):
    """Zero-dependency .env loader so secrets (OPENAI_API_KEY) stay in .env, not the
    launchd plist. Does not override already-set environment variables."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_load_env()

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from memnos_brain.store import BrainStore
from memnos_brain.service import MemnosMemory
from memnos_brain.control import Control
from memnos_brain import rerank as brain_rerank

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
PORT = int(os.environ.get("MEMNOS_PORT", "8900"))
POOL_MAX = int(os.environ.get("MEMNOS_POOL_MAX", "16"))
MAX_BODY = 256 * 1024          # 256 KB request cap
WRITE_OPS = {"/remember", "/consolidate"}

POOL = None
EMBED = None
LLM = None
DIM = 384


class _UsageAcc:
    """Per-request accumulator for engine LLM token usage (extraction + consolidation),
    converted to USD via the shared pricing table — so usage_ledger reflects real spend."""
    __slots__ = ("tin", "tout", "cost")

    def __init__(self):
        self.tin = self.tout = 0
        self.cost = 0.0

    def __call__(self, model, prompt_tokens, completion_tokens):
        from memnos_poc.usage import PRICING
        pin, pout = PRICING.get(model, (0.0, 0.0))
        self.tin += int(prompt_tokens or 0)
        self.tout += int(completion_tokens or 0)
        self.cost += (prompt_tokens or 0) / 1e6 * pin + (completion_tokens or 0) / 1e6 * pout


def _build_embedder():
    global LLM, DIM
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        from validate_brain import CachedEmbedder
        from locomo_pg_parallel import TSCostMeter
        # timeout is REQUIRED: without it a single stalled request hangs the request
        # thread forever (observed: extraction/consolidation wedged with no timeout).
        LLM = OpenAI(max_retries=3, timeout=60)
        DIM = 1536
        emb = CachedEmbedder(LLM, TSCostMeter())
        print("[memnos] OpenAI 1536-d embeddings + extraction ENABLED", flush=True)
        return emb
    from memnos_poc import local_models
    DIM = 384
    print("[memnos] local 384-d embeddings (free/private), no extraction", flush=True)
    return local_models.embed


class Handler(BaseHTTPRequestHandler):
    server_version = "memnos/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/readyz":
            try:
                with POOL.connection() as conn, conn.cursor() as c:
                    c.execute("SELECT 1")
                return self._send(200, {"ready": True})
            except Exception:
                return self._send(503, {"ready": False})
        if self.path.startswith("/metrics"):
            try:
                hours = 24
                with POOL.connection() as conn:
                    if Control.authenticate(conn, self._token()) is None:
                        return self._send(401, {"error": "unauthorized"})
                    rows = Control.stats(conn, hours)
                return self._send(200, {"window_hours": hours, "ops": rows})
            except Exception:
                traceback.print_exc()
                return self._send(500, {"error": "internal error"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        t0 = time.perf_counter()
        # --- body limits + json ---
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad content-length"})
        if n > MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid json"})

        ns = str(req.get("namespace", "")).strip()
        token = self._token()
        if not ns or len(ns) > 200:
            return self._send(400, {"error": "namespace required (<=200 chars)"})

        try:
            with POOL.connection() as conn:
                # --- auth ---
                principal = Control.authenticate(conn, token)
                if principal is None:
                    return self._send(401, {"error": "unauthorized"})
                # --- ACL ---
                write = self.path in WRITE_OPS
                if not Control.authorize(conn, principal, ns, write=write):
                    Control.audit(conn, principal, self.path.lstrip("/"), ns, False, {"reason": "forbidden"})
                    return self._send(403, {"error": "forbidden for namespace"})

                if self.path == "/feedback":   # the true quality signal: was recall helpful?
                    Control.record_feedback(conn, principal, ns, str(req.get("query", ""))[:1000],
                                            bool(req.get("helpful", False)), (str(req.get("note", "")) or None))
                    Control.audit(conn, principal, "feedback", ns, True,
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=200)
                    return self._send(200, {"ok": True})

                store = BrainStore(conn=conn)
                usage = _UsageAcc()        # captures extraction/consolidation LLM tokens+cost
                mem = MemnosMemory(store, EMBED, dim=DIM, llm=LLM, on_usage=usage)
                cost0 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                action = self.path.lstrip("/")
                try:
                    if self.path == "/remember":
                        text = str(req.get("text", "")).strip()
                        if not text or len(text) > 20000:
                            return self._send(400, {"error": "text required (<=20000 chars)"})
                        out = mem.remember(ns, text, speaker=req.get("speaker"), session_id=req.get("session_id"))
                    elif self.path == "/recall":
                        q = str(req.get("query", "")).strip()
                        if not q or len(q) > 4000:
                            return self._send(400, {"error": "query required (<=4000 chars)"})
                        # use the engine's CANONICAL (benchmarked) defaults; only override
                        # when the client explicitly supplies a value — no server-side drift.
                        rkw = {}
                        if "raw_quota" in req: rkw["raw_quota"] = int(req["raw_quota"])
                        if "fact_quota" in req: rkw["fact_quota"] = int(req["fact_quota"])
                        ckw = {"max_chars": int(req["max_chars"])} if "max_chars" in req else {}
                        rows = mem.recall(ns, q, **rkw)
                        out = {"memories": rows, "context": mem.context(ns, q, **rkw, **ckw)}
                    elif self.path == "/consolidate":
                        out = mem.consolidate(ns)
                    else:
                        return self._send(404, {"error": "not found"})
                except Exception as op_err:
                    # capture WHAT broke (type+message) so it's actionable via `memnos_admin.py errors`
                    Control.audit(conn, principal, action, ns, False,
                                  {"error": type(op_err).__name__, "msg": str(op_err)[:300]},
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=500)
                    traceback.print_exc()
                    return self._send(500, {"error": "internal error"})

                cost1 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                # full cost = embedding delta + extraction/consolidation LLM cost; tokens
                # = extraction tokens (the previously-untracked spend)
                Control.record_usage(conn, principal, ns, action, mem.extract_model,
                                     usage.tin, usage.tout, round((cost1 - cost0) + usage.cost, 6))
                rcount = len(out.get("memories", [])) if isinstance(out, dict) and "memories" in out else None
                Control.audit(conn, principal, action, ns, True,
                              latency_ms=int((time.perf_counter() - t0) * 1000), result_count=rcount, status=200)
                return self._send(200, out)
        except Exception:
            traceback.print_exc()              # pool/connection-level failure
            return self._send(500, {"error": "internal error"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    brain_rerank.rerank("warm", ["a", "b"])
    EMBED = _build_embedder()
    POOL = ConnectionPool(DSN, min_size=2, max_size=POOL_MAX, open=True,
                          kwargs={"autocommit": True, "row_factory": dict_row})
    with POOL.connection() as conn:
        BrainStore(conn=conn).create_schema("memnos", dim=DIM)   # memory schema
        Control.init(conn)                                        # control plane
        Control.audit(conn, None, "server_start", "-", True,      # heartbeat (uptime/crash-loop signal)
                      detail={"dim": DIM})
    print(f"[memnos] production server on http://127.0.0.1:{PORT} (pool max {POOL_MAX})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
