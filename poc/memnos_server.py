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
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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


def _load_config():
    """Load ~/.memnos/config.json (written by `memnos setup`) into env. setdefault, so an
    existing .env / real env still wins — but a fresh pip install with only config.json
    works with no .env. Shares the master key + DSN between CLI and server."""
    import json as _json
    p = os.path.join(os.path.expanduser("~"), ".memnos", "config.json")
    try:
        with open(p) as fh:
            cfg = _json.load(fh)
        if cfg.get("dsn"):
            os.environ.setdefault("MEMNOS_DSN", cfg["dsn"])
        if cfg.get("secret_key"):
            os.environ.setdefault("MEMNOS_SECRET_KEY", cfg["secret_key"])
        if cfg.get("port"):
            os.environ.setdefault("MEMNOS_PORT", str(cfg["port"]))
    except (FileNotFoundError, ValueError):
        pass


_load_env()
_load_config()

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
WRITE_OPS = {"/remember", "/consolidate", "/memory/write", "/memory/delete", "/corpus/ingest"}
_DELIVER_EVENT = threading.Event()      # set after a write → wakes the webhook pusher
WEBHOOK_TIMEOUT = float(os.environ.get("MEMNOS_WEBHOOK_TIMEOUT", "5"))
PUSHER_INTERVAL = float(os.environ.get("MEMNOS_PUSHER_INTERVAL", "3"))


def _webhook_post(url, payload):
    """POST a JSON payload to a subscriber webhook; raises on non-2xx (stdlib, no dep)."""
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(payload, default=str).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "memnos-webhook/1"})
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
        if not (200 <= r.status < 300):
            raise RuntimeError(f"webhook HTTP {r.status}")


def _pusher_loop():
    """Background webhook delivery: wakes on a write (event) or every PUSHER_INTERVAL,
    delivers pending events to subscriber webhooks at-least-once. Daemon thread."""
    while True:
        _DELIVER_EVENT.wait(timeout=PUSHER_INTERVAL)
        _DELIVER_EVENT.clear()
        try:
            with POOL.connection() as conn:
                Control.deliver_pending(conn, _webhook_post)
        except Exception:
            traceback.print_exc()
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_CTYPE = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}

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
        body = json.dumps(obj, default=str).encode()   # default=str → datetime/Decimal safe
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def _send_static(self, fname):
        """Serve the zero-build console from poc/ui/ (localhost-only shell; JS does auth)."""
        safe = os.path.basename(fname)                     # no path traversal
        fp = os.path.join(UI_DIR, safe)
        if not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        body = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", _CTYPE.get(os.path.splitext(safe)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n > MAX_BODY:
            return None
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            return req if isinstance(req, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _admin(self, method, sub, qs, body):
        """Management-console API under /admin/api/. Requires an ADMIN principal
        (holds the '*' grant). Returns (code, obj). Audited by the caller."""
        with POOL.connection() as conn:
            pid = Control.authenticate(conn, self._token())
            if pid is None:
                return 401, {"error": "unauthorized"}
            if not Control.is_admin(conn, pid):
                return 403, {"error": "admin token required ('*' grant)"}
            body = body or {}
            try:
                if sub == "namespaces" and method == "GET":
                    return 200, {"namespaces": Control.list_namespaces(conn)}
                if sub == "namespaces" and method == "POST":
                    name = str(body.get("name", "")).strip()
                    if not name or len(name) > 200:
                        return 400, {"error": "name required (<=200 chars)"}
                    Control.create_namespace(conn, name, created_by=pid, description=body.get("description"))
                    return 200, {"ok": True, "name": name}
                if sub == "namespaces" and method == "DELETE":
                    name = (qs.get("name", [""])[0]).strip()
                    if not name:
                        return 400, {"error": "name required"}
                    Control.delete_namespace(conn, name, purge_data=qs.get("purge", ["0"])[0] == "1")
                    return 200, {"ok": True}
                if sub == "principals" and method == "GET":
                    return 200, {"principals": Control.list_principals(conn)}
                if sub == "principals" and method == "POST":
                    name = str(body.get("name", "")).strip()
                    if not name:
                        return 400, {"error": "name required"}
                    npid = Control.create_principal(conn, name, body.get("kind", "user"))
                    return 200, {"ok": True, "id": npid}
                if sub == "tokens" and method == "GET":
                    p = int(qs.get("principal", [0])[0])
                    return 200, {"tokens": Control.list_tokens(conn, p)}
                if sub == "tokens" and method == "POST":
                    p = int(body.get("principal_id", 0))
                    tok = Control.mint_token(conn, p, body.get("label"), body.get("ttl_days"))
                    return 200, {"token": tok}          # plaintext ONCE
                if sub == "tokens/revoke" and method == "POST":
                    Control.revoke_token_by_id(conn, int(body.get("id", 0)))
                    return 200, {"ok": True}
                if sub == "grants" and method == "POST":
                    Control.grant(conn, int(body.get("principal_id", 0)), str(body.get("namespace", "")),
                                  bool(body.get("can_read", True)), bool(body.get("can_write", True)))
                    return 200, {"ok": True}
                if sub == "grants" and method == "DELETE":
                    Control.revoke_grant(conn, int(qs.get("principal", [0])[0]), qs.get("namespace", [""])[0])
                    return 200, {"ok": True}
                if sub == "grants" and method == "GET":
                    return 200, {"grants": Control.authorized_namespaces(conn, int(qs.get("principal", [0])[0]))}
                if sub == "stats" and method == "GET":
                    return 200, {"ops": Control.stats(conn)}
                if sub == "usage" and method == "GET":
                    return 200, {"usage": Control.usage_rollup(conn)}
                if sub == "audit" and method == "GET":
                    return 200, {"audit": Control.recent_audit(conn, int(qs.get("limit", [50])[0]))}
                if sub == "health" and method == "GET":
                    return 200, {"findings": [{"level": l, "msg": m} for l, m in Control.health(conn)]}
                if sub == "quality" and method == "GET":
                    return 200, {"trend": Control.eval_trend(conn, "stale_suppression", "rate", 10)}
                if sub == "subscriptions" and method == "GET":
                    return 200, {"subscriptions": Control.list_subscriptions(conn, int(qs.get("principal", [pid])[0]))}
                if sub == "deliver" and method == "POST":
                    # run a webhook delivery pass now (ops + deterministic tests)
                    return 200, {"delivered": Control.deliver_pending(conn, _webhook_post)}
                if sub == "provider" and method == "GET":
                    from memnos_brain.vault import Vault
                    return 200, {"mode": "openai" if os.environ.get("OPENAI_API_KEY") else "local",
                                 "dim": DIM, "key_present": bool(os.environ.get("OPENAI_API_KEY")),
                                 "extract_model": "gpt-4o-mini", "vault_unlocked": Vault.available()}
                if sub == "secrets":
                    from memnos_brain.vault import Vault, VaultLocked
                    try:
                        if method == "GET":
                            return 200, {"secrets": Vault.list(conn), "unlocked": Vault.available()}
                        if method == "POST":
                            name = str(body.get("name", "")).strip()
                            val = str(body.get("value", ""))
                            if not name or not val:
                                return 400, {"error": "name and value required"}
                            Vault.set(conn, name, val, body.get("description"))   # plaintext never stored
                            return 200, {"ok": True, "name": name}
                        if method == "DELETE":
                            Vault.delete(conn, (qs.get("name", [""])[0]).strip())
                            return 200, {"ok": True}
                    except VaultLocked as v:
                        return 409, {"error": "vault locked", "msg": str(v)}
            except Exception as e:
                traceback.print_exc()
                return 500, {"error": type(e).__name__, "msg": str(e)[:200]}
            return 404, {"error": "unknown admin route"}

    def do_GET(self):
        u = urlparse(self.path)
        # --- management console (zero-build static UI) ---
        if u.path in ("/admin", "/admin/"):
            return self._send_static("index.html")
        if u.path.startswith("/admin/api/"):
            code, obj = self._admin("GET", u.path[len("/admin/api/"):], parse_qs(u.query), None)
            return self._send(code, obj)
        if u.path.startswith("/admin/"):
            return self._send_static(u.path[len("/admin/"):])
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

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/admin/api/"):
            code, obj = self._admin("DELETE", u.path[len("/admin/api/"):], parse_qs(u.query), None)
            return self._send(code, obj)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        # --- management console admin API ---
        if u.path.startswith("/admin/api/"):
            body = self._read_body()
            if body is None:
                return self._send(400, {"error": "invalid json"})
            code, obj = self._admin("POST", u.path[len("/admin/api/"):], parse_qs(u.query), body)
            return self._send(code, obj)

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
                    # --- memory CRUD parity (master/ArcadeDB feature parity) ---
                    elif self.path == "/memory/write":     # alias of /remember
                        text = str(req.get("text", "") or req.get("content", "")).strip()
                        if not text or len(text) > 20000:
                            return self._send(400, {"error": "text/content required (<=20000 chars)"})
                        out = mem.remember(ns, text, speaker=req.get("speaker"), session_id=req.get("session_id"))
                    elif self.path in ("/memory/search", "/recall_v2"):   # alias of /recall (memory_search)
                        q = str(req.get("query", "")).strip()
                        if not q or len(q) > 4000:
                            return self._send(400, {"error": "query required (<=4000 chars)"})
                        rkw = {}
                        if "raw_quota" in req: rkw["raw_quota"] = int(req["raw_quota"])
                        if "fact_quota" in req: rkw["fact_quota"] = int(req["fact_quota"])
                        out = {"memories": mem.recall(ns, q, **rkw)}
                    elif self.path == "/memory/context":   # ready-to-paste context block
                        q = str(req.get("query", "")).strip()
                        if not q or len(q) > 4000:
                            return self._send(400, {"error": "query required (<=4000 chars)"})
                        ckw = {"max_chars": int(req["max_chars"])} if "max_chars" in req else {}
                        out = {"context": mem.context(ns, q, **ckw)}
                    elif self.path == "/memory/delete":    # expire a semantic fact by id (system-time)
                        try:
                            sid = int(req.get("id"))
                        except (TypeError, ValueError):
                            return self._send(400, {"error": "id (int) required"})
                        existing = store.get_semantic(mem.schema, ns, sid)
                        if not existing:
                            return self._send(404, {"error": "fact not found in namespace"})
                        store.expire(mem.schema, ns, sid)
                        out = {"deleted": sid, "statement": existing.get("statement")}
                    # --- graph read (entities/edges already populated; no API existed) ---
                    elif self.path == "/entity":           # get_entity
                        name = str(req.get("name", "")).strip()
                        if not name:
                            return self._send(400, {"error": "name required"})
                        depth = max(1, min(int(req.get("depth", 1)), 3))
                        res = store.get_entity(mem.schema, ns, name, depth=depth)
                        if res is None:
                            return self._send(404, {"error": "entity not found"})
                        out = res
                    elif self.path == "/related":          # get_related (adjacency)
                        name = str(req.get("name", "")).strip()
                        if not name:
                            return self._send(400, {"error": "name required"})
                        out = {"name": name, "related": store.get_related(mem.schema, ns, name)}
                    elif self.path == "/graph":            # graph_query (N-hop expand → facts)
                        ents = req.get("entities") or ([req["name"]] if req.get("name") else [])
                        ents = [str(e) for e in ents if str(e).strip()][:10]
                        if not ents:
                            return self._send(400, {"error": "entities[] or name required"})
                        hops = max(1, min(int(req.get("hops", 2)), 3))
                        out = {"facts": store.graph_expand(mem.schema, ns, ents, hops=hops,
                                                           limit=int(req.get("limit", 20)))}
                    # --- communities / contradictions / knowledge health (Batch 2) ---
                    elif self.path == "/community":        # community_search
                        name = str(req.get("name", "")).strip()
                        if not name:
                            return self._send(400, {"error": "name required"})
                        res = store.community(mem.schema, ns, name)
                        if res is None:
                            return self._send(404, {"error": "entity not found"})
                        out = res
                    elif self.path == "/contradictions":   # check_contradictions
                        out = {"contradictions": store.contradictions(mem.schema, ns)}
                    elif self.path == "/knowledge/health":  # knowledge_health (namespace)
                        out = store.health(mem.schema, ns)
                    # --- namespace pub/sub (Batch 3) ---
                    elif self.path == "/subscribe":        # namespace_subscribe
                        wh = (str(req.get("webhook")).strip() or None) if req.get("webhook") else None
                        out = Control.subscribe(conn, principal, ns, wh)
                    elif self.path == "/feed":             # namespace_feed (poll since cursor)
                        try:
                            sid = int(req.get("subscription_id"))
                        except (TypeError, ValueError):
                            return self._send(400, {"error": "subscription_id (int) required"})
                        res = Control.feed(conn, principal, sid, ns, limit=int(req.get("limit", 50)))
                        if res is None:
                            return self._send(404, {"error": "subscription not found for this principal/namespace"})
                        out = res
                    elif self.path == "/unsubscribe":
                        try:
                            sid = int(req.get("subscription_id"))
                        except (TypeError, ValueError):
                            return self._send(400, {"error": "subscription_id (int) required"})
                        out = {"unsubscribed": Control.unsubscribe(conn, principal, sid)}
                    # --- corpus ingestion + checks (Batch 4) ---
                    elif self.path == "/corpus/ingest":    # parse doc constraints -> facts
                        name = str(req.get("name", "")).strip()
                        text = str(req.get("text", ""))
                        if not name or not text.strip():
                            return self._send(400, {"error": "name and text required"})
                        ids = store.ingest_constraints(mem.schema, ns, name, text)
                        Control.corpus_record(conn, ns, name, str(req.get("kind", "doc")),
                                              (str(req.get("git_sha")) or None) if req.get("git_sha") else None,
                                              len(ids))
                        out = {"source": name, "constraints": len(ids), "ids": ids}
                    elif self.path == "/corpus/check":      # constraints relevant to a snippet
                        snippet = str(req.get("snippet", "") or req.get("code", ""))
                        if not snippet.strip():
                            return self._send(400, {"error": "snippet/code required"})
                        out = {"constraints": store.corpus_check(mem.schema, ns, snippet)}
                    elif self.path == "/corpus/list":
                        out = {"sources": Control.corpus_list(conn, ns)}
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
                if self.path in WRITE_OPS:
                    _DELIVER_EVENT.set()       # wake the webhook pusher (near-immediate push)
                return self._send(200, out)
        except Exception:
            traceback.print_exc()              # pool/connection-level failure
            return self._send(500, {"error": "internal error"})

    def log_message(self, *a):
        pass


def serve(port=None):
    """Boot + run the memnos server. Importable so the `memnos serve` CLI reuses it."""
    global POOL, EMBED
    port = int(port or PORT)
    brain_rerank.rerank("warm", ["a", "b"])
    POOL = ConnectionPool(DSN, min_size=2, max_size=POOL_MAX, open=True,
                          kwargs={"autocommit": True, "row_factory": dict_row})
    with POOL.connection() as conn:
        Control.init(conn)                                        # control plane (incl. secrets table)
        # provider key may be a vault value-ref (secret://name) — resolve before building embedder
        k = os.environ.get("OPENAI_API_KEY", "")
        if k.startswith("secret://"):
            try:
                from memnos_brain.vault import Vault
                rk = Vault.resolve(conn, k)
                if rk:
                    os.environ["OPENAI_API_KEY"] = rk
                    print("[memnos] OPENAI_API_KEY resolved from vault", flush=True)
            except Exception as e:
                print(f"[memnos] WARN: could not resolve provider key from vault: {e}", flush=True)
    EMBED = _build_embedder()
    with POOL.connection() as conn:
        BrainStore(conn=conn).create_schema("memnos", dim=DIM)   # memory schema
        Control.audit(conn, None, "server_start", "-", True,      # heartbeat (uptime/crash-loop signal)
                      detail={"dim": DIM})
    threading.Thread(target=_pusher_loop, name="memnos-webhook-pusher", daemon=True).start()
    print(f"[memnos] production server on http://127.0.0.1:{port} (pool max {POOL_MAX}); webhook pusher on", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
