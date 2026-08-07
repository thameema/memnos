"""memnos production memory server — pooled, authenticated, namespace-ACL'd, audited.

Hardening vs the early prototype:
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
import asyncio
import base64
import io
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import httpx

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
        if cfg.get("openai"):                         # 'secret://openai' — resolved at startup
            os.environ.setdefault("OPENAI_API_KEY", cfg["openai"])
    except (FileNotFoundError, ValueError):
        pass


_load_env()
_load_config()

from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from core.store import BrainStore, query_clamp, RECALL_ARM_FAILURES
from core.service import MemnosMemory
from core.control import Control
from core import rerank as brain_rerank
from core import memrelief

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_PORT", "8900"))
POOL_MAX = int(os.environ.get("MEMNOS_POOL_MAX", "16"))
# Extraction model: gpt-4o-mini on the OpenAI path; override for OpenAI-compatible
# local servers (e.g. MEMNOS_EXTRACT_MODEL=llama3.2:3b with MEMNOS_EXTRACT_BASE_URL).
EXTRACT_MODEL = os.environ.get("MEMNOS_EXTRACT_MODEL", "gpt-4o-mini")
MAX_BODY = 256 * 1024          # 256 KB request cap
# issue #15: recall queries are CLAMPED to this many chars (not 400-rejected) so a long
# query returns 200 — the embedder truncates and the FTS arm is token-clamped, so it is
# safe. Generous default; only a genuinely abusive payload exceeds it.
_QUERY_MAX_CHARS = int(os.environ.get("MEMNOS_QUERY_MAX_CHARS", "20000"))
# issue #41 fix C: degraded_reasons has no natural bound (a pathological wide recall
# could fan out to many namespaces, each contributing its own entry) — cap what reaches
# the client/audit ledger so one bad recall can't inflate the response/ledger row size.
# The full, uncapped detail is still in the server log (each failure is printed there
# before this cap is ever applied).
_DEGRADED_REASONS_CAP = 25
# issue #14: optional budget thresholds — float USD or None (unconfigured)
def _parse_budget_env(name):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None
BUDGET_DAILY_USD = _parse_budget_env("MEMNOS_BUDGET_DAILY_USD")
BUDGET_MONTHLY_USD = _parse_budget_env("MEMNOS_BUDGET_MONTHLY_USD")
WRITE_OPS = {"/remember", "/consolidate", "/memory/write", "/memory/delete", "/corpus/ingest",
             "/ingest/file", "/episode/segment", "/episode/decay", "/namespace/copy",
             "/lease/acquire", "/lease/heartbeat", "/lease/release"}


def _suggest_enabled():
    """Suggest-on-mismatch is ON by default; MEMNOS_SUGGEST_NAMESPACE=0 turns it off so a
    deployment that finds it noisy (or wants zero extra per-write SQL) can opt out."""
    return os.environ.get("MEMNOS_SUGGEST_NAMESPACE", "1").strip().lower() not in ("0", "false", "no", "off")


def _write_suggestion(conn, principal_id, write_ns, facts, raw_text):
    """Build the non-blocking suggest-on-mismatch hint for a /remember response, or None.
    Gathers the write's entity names (each fact's subject + cheap proper-noun NER over the
    raw turn — the SAME signal the encoder writes as entities/mentions) and asks the bounded
    Control.suggest_namespace helper whether a DIFFERENT writable namespace dominates them.
    SUGGEST ONLY — never moves the write. Best-effort: any error -> None (a write must NEVER
    fail because the advisory broke)."""
    if not _suggest_enabled():
        return None
    try:
        from core.encode import extract_entities
        names = set(extract_entities(raw_text or ""))
        for f in (facts or []):
            s = (f.get("subject") or "").strip()
            if s:
                names.add(s)
        return Control.suggest_namespace(conn, principal_id, write_ns, list(names))
    except Exception:
        return None
# Endpoints whose handler mixes DB work with SLOW NON-DB work (network embeddings, LLM
# calls, cross-encoder rerank, file parsing). These are served by the PHASED dispatcher
# (_phased): pool connections are only held for the short DB phases — never across model
# I/O. Everything left in the generic do_POST block below is pure SQL.
PHASED_OPS = {"/recall", "/memory/search", "/recall_v2", "/memory/context", "/consolidate",
              "/ingest/file", "/episode/segment", "/episode/recall", "/reconcile"}
# TYPED MEMORIES (0.1.6): the closed set of memory types. Validated server-side on every
# write (400 on anything else) so the column can never accumulate junk labels.
# type='constraint' memories are PINNED into /recall (see _phased_op).
ALLOWED_MEMORY_TYPES = ("decision", "incident", "constraint", "skill", "fact")


def _memory_type(req):
    """Parse + validate the optional `type` field. Returns (ok, value_or_error_obj):
    (True, None) when absent, (True, t) when valid, (False, {error}) on an unknown type."""
    t = req.get("type")
    if t is None or str(t).strip() == "":
        return True, None
    t = str(t).strip().lower()
    if t not in ALLOWED_MEMORY_TYPES:
        return False, {"error": "unknown type %r (allowed: %s)" % (t, " | ".join(ALLOWED_MEMORY_TYPES))}
    return True, t


def _chunk_text(text, size=1200, overlap=150):
    """Paragraph-aware chunking: pack paragraphs up to ~size chars; hard-split any
    paragraph longer than size (with overlap). Keeps semantically-coherent chunks."""
    text = (text or "").strip()
    if not text:
        return []
    chunks, cur = [], ""
    for p in re.split(r"\n\s*\n", text):
        p = p.strip()
        if not p:
            continue
        if len(p) > size:
            if cur:
                chunks.append(cur); cur = ""
            for i in range(0, len(p), max(1, size - overlap)):
                chunks.append(p[i:i + size])
        elif len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n" + p).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def _extract_file_text(filename, raw: bytes):
    """Best-effort text extraction. PDF/DOCX need pypdf/python-docx (optional); everything
    else is decoded as UTF-8 text (md/txt/code). Returns None if it can't be parsed."""
    low = (filename or "").lower()
    try:
        if low.endswith(".pdf"):
            import pypdf
            r = pypdf.PdfReader(io.BytesIO(raw))
            return "\n\n".join((pg.extract_text() or "") for pg in r.pages)
        if low.endswith(".docx"):
            import docx
            d = docx.Document(io.BytesIO(raw))
            return "\n\n".join(p.text for p in d.paragraphs)
        return raw.decode("utf-8", "replace")
    except Exception:
        return None
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
    delivers pending events to subscriber webhooks at-least-once. Daemon thread.
    conn_factory=POOL.connection: DB steps each use a short-lived pool conn so NO pool
    slot is held across the webhook POSTs (network, up to WEBHOOK_TIMEOUT each)."""
    while True:
        _DELIVER_EVENT.wait(timeout=PUSHER_INTERVAL)
        _DELIVER_EVENT.clear()
        try:
            Control.deliver_pending(None, _webhook_post, conn_factory=POOL.connection)
        except (PoolTimeout, OperationalError) as e:
            print(f"[memnos] webhook pusher: DB unreachable ({type(e).__name__}) — will retry", flush=True)
        except Exception:
            traceback.print_exc()
def _find_ui_dir():
    """Locate the /admin console assets in both source and installed (pip/pipx) layouts —
    next to the module (source/editable) or under <prefix>/share/memnos/ui (data-files)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, "ui"),
              os.path.join(sys.prefix, "share", "memnos", "ui"),
              os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "share", "memnos", "ui")):
        if os.path.isdir(c):
            return c
    return os.path.join(here, "ui")


UI_DIR = _find_ui_dir()
_CTYPE = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
          ".json": "application/json"}

POOL = None
_NS_CACHE = {"t": 0.0, "data": None}    # namespace-census cache (10s TTL, write-invalidated)
_INGEST_Q = queue.Queue(maxsize=1024)   # async /remember extraction queue
EMBED = None
LLM = None
DIM = 384

# Issue #12 — recall latency. Query-embed cache: hooks/Desktop repeat near-identical
# queries within seconds; a short-TTL LRU keyed (query, model) lets repeats skip the
# OpenAI round-trip entirely. Embed executor: the round-trip for cache MISSES is
# overlapped with the non-vector DB arms (recall_prefetch) — never with a pool conn held.
from core.embed import QueryEmbedCache


def _env_f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_QUERY_CACHE = QueryEmbedCache(ttl_s=_env_f("MEMNOS_QUERY_CACHE_TTL_S", 60.0))
_EMBED_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="memnos-query-embed")


# --- issue #37 Layer 1: streamable-HTTP MCP transport, mounted alongside the existing
# stdio adapter (memnos_mcp.py) and this REST API. Purely additive — the stdio adapter
# and every REST route above are unchanged.
#
# WHY a second internal listener instead of mounting a Starlette route directly on this
# server: this Handler is a synchronous http.server (BaseHTTPRequestHandler /
# ThreadingHTTPServer), but FastMCP's streamable-http transport is ASGI-only and its
# stateless-http session manager needs ONE long-lived event loop that owns its Starlette
# lifespan (a fresh asyncio.run() per request breaks its internal task group — verified
# empirically). So a real uvicorn+Starlette stack runs the MCP app on a LOOPBACK-ONLY
# internal port, in a dedicated thread with its own persistent loop; the public :8900
# Handler reverse-proxies /mcp requests to it (Handler._forward_mcp below). :8900 stays
# the only externally-visible port; REST auth/ACL/audit/pool code is untouched.
MCP_INTERNAL_PORT = None    # resolved in start_mcp_http_mount() against the ACTUAL runtime
                             # port (serve(port=...) can override the MEMNOS_PORT default)
_MCP_THREAD = None


def _mcp_asgi_app(public_port):
    """The streamable-HTTP ASGI app, built from memnos_mcp's FastMCP server — the SAME
    @mcp.tool() functions that back the stdio adapter (memnos_mcp.mcp), per issue #37's
    'mount the SAME tool definitions' requirement. Wrapped with a raw-ASGI edge-auth layer:
    verifies the caller's Bearer token via Control.authenticate (the existing token/
    principal model — the SAME check every REST endpoint below already does) before an
    MCP session is allowed to start, then stashes (token, namespace) in
    memnos_mcp._REQUEST_CTX so the tool functions forward the CALLER's OWN token back to
    this same REST API (see memnos_mcp._post/_ns_source) — zero duplicated auth/ACL/audit
    logic, and namespace grants are enforced exactly as they are for any other caller."""
    import memnos_mcp
    # Force the loopback target to THIS instance's actual runtime port — memnos_mcp.URL
    # otherwise reads MEMNOS_URL from the environment at import time (meant for the stdio
    # adapter, spawned as its own process with its own env), which may be unset or point
    # at a different memnos instance entirely when running under a custom `serve(port=)`.
    memnos_mcp.URL = f"http://127.0.0.1:{public_port}"
    inner = memnos_mcp.mcp.streamable_http_app()

    async def _send_json(send, status, obj):
        body = json.dumps(obj).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def _authenticate(token, ns_header):
        """Runs in a worker thread (asyncio.to_thread) — psycopg/POOL calls are sync."""
        with POOL.connection() as conn:
            pid = Control.authenticate(conn, token)
            if pid is None:
                return None, None, 0
            ns = (ns_header or "").strip()
            grants = []
            if not ns:
                # No explicit namespace header (agent-setup --transport http always sends
                # one; this is a fallback for hand-rolled clients): if the token is
                # granted on exactly one concrete (non-wildcard) namespace, default to it.
                # READ-grant-based on purpose — a read-only single-namespace token still
                # gets a resolved namespace to recall from; a subsequent write correctly
                # 403s downstream (REST enforces write grants exactly as for any caller),
                # it just does so via the normal write path rather than at this fallback.
                grants = [g["namespace"] for g in Control.authorized_namespaces(conn, pid)
                         if g["can_read"] and g["namespace"] != "*" and not g["namespace"].endswith(":*")]
                if len(grants) == 1:
                    ns = grants[0]
            return pid, ns, len(grants)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                  for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        pid, ns, ngrants = await asyncio.to_thread(_authenticate, token, headers.get("x-memnos-namespace"))
        if pid is None:
            return await _send_json(send, 401, {"error": "unauthorized"})
        if not ns:
            reason = ("token has no read-granted namespaces" if ngrants == 0
                      else "token is granted on more than one namespace")
            return await _send_json(send, 400, {"error": f"X-Memnos-Namespace header required ({reason})"})
        rctx = memnos_mcp._REQUEST_CTX.set((token, ns))
        try:
            await inner(scope, receive, send)
        finally:
            memnos_mcp._REQUEST_CTX.reset(rctx)

    return app


def _run_mcp_http_server(public_port, sock, server_holder):
    import uvicorn
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(_mcp_asgi_app(public_port), log_level="warning")
    server = uvicorn.Server(config)
    server_holder["server"] = server   # so the caller's thread can poll server.started
    # serve()'s signal-handling is a documented no-op off the main thread (uvicorn checks
    # threading.current_thread() itself) — safe to call directly here.
    loop.run_until_complete(server.serve(sockets=[sock]))


def start_mcp_http_mount(public_port):
    """Boot the internal streamable-HTTP MCP server in a background daemon thread and
    block until it's accepting connections. `public_port` is the ACTUAL port this server
    instance is binding to (serve(port=...) can differ from the MEMNOS_PORT default).

    The internal listener binds an OS-assigned ephemeral port (port 0) by default — NOT
    a fixed public_port+1 offset. A fixed offset is a real collision hazard on any
    machine already running other services (verified: it collided with an unrelated,
    already-running memnos instance during development) — ephemeral binding can't
    collide with anything. MEMNOS_MCP_INTERNAL_PORT still overrides to a fixed port if
    a deployment specifically needs one (e.g. a firewall rule scoped to one port)."""
    global _MCP_THREAD, MCP_INTERNAL_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    env_port = os.environ.get("MEMNOS_MCP_INTERNAL_PORT")
    sock.bind(("127.0.0.1", int(env_port) if env_port else 0))
    sock.listen(128)
    MCP_INTERNAL_PORT = sock.getsockname()[1]      # known immediately — no bind-race to probe for
    server_holder = {}
    _MCP_THREAD = threading.Thread(target=_run_mcp_http_server, args=(public_port, sock, server_holder),
                                   name="memnos-mcp-http", daemon=True)
    _MCP_THREAD.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _MCP_THREAD.is_alive():
            raise RuntimeError("memnos MCP HTTP mount thread died during startup")
        if server_holder.get("server") is not None and server_holder["server"].started:
            return
        time.sleep(0.05)
    raise RuntimeError(f"memnos MCP HTTP mount failed to start on 127.0.0.1:{MCP_INTERNAL_PORT}")


class _UsageAcc:
    """Per-request accumulator for engine LLM token usage (extraction + consolidation),
    converted to USD via the shared pricing table — so usage_ledger reflects real spend."""
    __slots__ = ("tin", "tout", "cost")

    def __init__(self):
        self.tin = self.tout = 0
        self.cost = 0.0

    def __call__(self, model, prompt_tokens, completion_tokens):
        from core.usage import PRICING
        pin, pout = PRICING.get(model, (0.0, 0.0))
        self.tin += int(prompt_tokens or 0)
        self.tout += int(completion_tokens or 0)
        self.cost += (prompt_tokens or 0) / 1e6 * pin + (completion_tokens or 0) / 1e6 * pout


def _fake_extract(text, date):
    """TEST-ONLY deterministic fact extractor (MEMNOS_FAKE_EXTRACT=1). Emits one SPO fact
    per proper-noun token found by the regex NER — so `write_facts` persists those entities
    into the write namespace, EXACTLY as the real LLM path does. This lets CI (no OpenAI
    key) exercise the genuine P2(extract)->P3(write_facts)->suggestion ORDERING — the only
    path where the self-pollution bug lived. NEVER enable in production: it captures noise,
    not real facts."""
    from core.encode import extract_entities
    seen, facts = set(), []
    for e in extract_entities(text or ""):
        k = (e or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        facts.append({"subject": e, "predicate": "", "object": "",
                      "statement": f"{e} was mentioned."})
    return facts


# When set, the server runs the FULL extraction branch with the $0 deterministic extractor
# above instead of an LLM — used only by tests/CI to reproduce the LLM-path write ordering.
EXTRACT_FN = _fake_extract if os.environ.get("MEMNOS_FAKE_EXTRACT", "").strip().lower() in (
    "1", "true", "yes", "on") else None


def _have_extraction():
    """True when the /remember path should run extraction (real LLM OR the test extractor)."""
    return LLM is not None or EXTRACT_FN is not None


def _build_embedder():
    global LLM, DIM
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        from core.embed import CachedEmbedder
        from core.usage import TSCostMeter
        # timeout is REQUIRED: without it a single stalled request hangs the request
        # thread forever (observed: extraction/consolidation wedged with no timeout).
        LLM = OpenAI(max_retries=3, timeout=60)
        DIM = 1536
        emb = CachedEmbedder(LLM, TSCostMeter())
        print("[memnos] OpenAI 1536-d embeddings + extraction ENABLED", flush=True)
        return emb
    from core import local_models
    DIM = 384
    # Local LLM extraction via any OpenAI-compatible endpoint (e.g. Ollama at
    # http://localhost:11434/v1) WITHOUT switching embeddings off the free local-384
    # path. Set MEMNOS_EXTRACT_BASE_URL (+ optional MEMNOS_EXTRACT_MODEL) and no
    # OPENAI_API_KEY: embeddings stay local/free, only fact extraction calls the LLM.
    if os.environ.get("MEMNOS_EXTRACT_BASE_URL"):
        from openai import OpenAI
        # OpenAI-compatible servers (Ollama, vLLM, LM Studio) ignore the key but the
        # SDK requires one — a placeholder is fine and never leaves the box.
        LLM = OpenAI(base_url=os.environ["MEMNOS_EXTRACT_BASE_URL"],
                     api_key=os.environ.get("MEMNOS_EXTRACT_API_KEY", "local"),
                     max_retries=2, timeout=120)
        print(f"[memnos] local 384-d embeddings (free/private) + LOCAL LLM extraction "
              f"via {os.environ['MEMNOS_EXTRACT_BASE_URL']} model={EXTRACT_MODEL}", flush=True)
        return local_models.embed
    print("[memnos] local 384-d embeddings (free/private), no extraction", flush=True)
    return local_models.embed


class Handler(BaseHTTPRequestHandler):
    server_version = "memnos/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()   # default=str → datetime/Decimal safe
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                                       # client timed out / gave up — nothing to send to

    def _token(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def _forward_mcp(self, method):
        """Reverse-proxy /mcp to the internal streamable-HTTP MCP listener (see
        start_mcp_http_mount above) — body + headers in, status/headers/body out,
        unchanged. The internal app does its own edge auth; a 503 here means the
        internal listener itself is down (startup race or crash), not an auth failure."""
        if "Content-Length" not in self.headers and "Transfer-Encoding" in self.headers:
            # No client we forward for uses chunked transfer (all set Content-Length) —
            # reject explicitly rather than silently forwarding an empty body, which would
            # surface as a confusing malformed-JSON-RPC error one hop downstream instead.
            return self._send(400, {"error": "chunked Transfer-Encoding not supported on /mcp — "
                                    "send Content-Length"})
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n else b""
        if MCP_INTERNAL_PORT is None:
            # start_mcp_http_mount() never came up (see serve()) — degrade cleanly rather
            # than building a URL around a nonexistent port. REST/stdio are unaffected.
            return self._send(503, {"error": "memnos MCP-HTTP mount is not running (failed "
                                    "to start at boot — see server logs); REST and the "
                                    "stdio adapter remain fully available"})
        fwd_headers = {k: v for k, v in self.headers.items()
                      if k.lower() not in ("host", "content-length", "connection", "transfer-encoding")}
        url = f"http://127.0.0.1:{MCP_INTERNAL_PORT}{self.path}"
        try:
            r = httpx.request(method, url, content=body, headers=fwd_headers, timeout=60)
        except httpx.TransportError:
            return self._send(503, {"error": "memnos MCP transport unavailable"})
        try:
            self.send_response(r.status_code)
            for hk, hv in r.headers.items():
                if hk.lower() in ("content-length", "connection", "transfer-encoding"):
                    continue
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_static(self, fname):
        """Serve the zero-build console from ui/ (localhost-only shell; JS does auth)."""
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
                    # short TTL cache: the console fires several parallel calls on page
                    # load; the namespace census aggregates large tables — compute once
                    now = time.monotonic()
                    if _NS_CACHE["data"] is None or now - _NS_CACHE["t"] > 10:
                        _NS_CACHE["data"] = Control.list_namespaces(conn)
                        _NS_CACHE["t"] = now
                    return 200, {"namespaces": _NS_CACHE["data"]}
                if sub == "namespaces" and method == "POST":
                    name = str(body.get("name", "")).strip()
                    if not name or len(name) > 200:
                        return 400, {"error": "name required (<=200 chars)"}
                    Control.create_namespace(conn, name, created_by=pid, description=body.get("description"))
                    if body.get("kind"):
                        Control.set_namespace_kind(conn, name, str(body["kind"]))
                    _NS_CACHE["data"] = None
                    return 200, {"ok": True, "name": name}
                # --- grounded-recall namespace links (admin-only CRUD) ---
                if sub == "namespaces/links" and method == "GET":
                    src = (qs.get("ns", [""])[0]).strip() or None
                    return 200, {"links": Control.list_links(conn, src)}
                if sub == "namespaces/links" and method == "POST":
                    src = str(body.get("src", "")).strip()
                    dst = str(body.get("dst", "")).strip()
                    if not src or not dst:
                        return 400, {"error": "src and dst required"}
                    if src == dst:
                        return 400, {"error": "src and dst must differ"}
                    Control.link_namespaces(conn, src, dst, created_by=pid)
                    return 200, {"ok": True, "src": src, "dst": dst}
                if sub == "namespaces/links" and method == "DELETE":
                    src = (qs.get("src", [""])[0]).strip()
                    dst = (qs.get("dst", [""])[0]).strip()
                    if not src or not dst:
                        return 400, {"error": "src and dst required"}
                    return 200, {"ok": True, "removed": Control.unlink_namespaces(conn, src, dst)}
                if sub == "namespaces/kind" and method == "POST":
                    name = str(body.get("name", "")).strip()
                    kind = str(body.get("kind", "")).strip()
                    if not name or kind not in ("memory", "knowledge"):
                        return 400, {"error": "name and kind ('memory'|'knowledge') required"}
                    Control.set_namespace_kind(conn, name, kind)
                    _NS_CACHE["data"] = None
                    return 200, {"ok": True, "name": name, "kind": kind}
                if sub == "namespaces" and method == "DELETE":
                    name = (qs.get("name", [""])[0]).strip()
                    if not name:
                        return 400, {"error": "name required"}
                    Control.delete_namespace(conn, name, purge_data=qs.get("purge", ["0"])[0] == "1")
                    _NS_CACHE["data"] = None
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
                    hours = max(1, min(int(qs.get("hours", [24])[0]), 720))
                    return 200, {"ops": Control.stats(conn, hours), "window_hours": hours}
                if sub == "usage" and method == "GET":
                    days = qs.get("days", [None])[0]
                    days = max(1, min(int(days), 365)) if days else 30
                    ns_filter = (qs.get("namespace", [""])[0]).strip() or None
                    return 200, {**Control.usage_summary(conn, days, ns_filter),
                                 "period_days": days, "namespace": ns_filter}
                if sub == "budget" and method == "GET":
                    return 200, Control.budget_status(conn, BUDGET_DAILY_USD, BUDGET_MONTHLY_USD)
                if sub == "memory/feed" and method == "GET":
                    # recent memories across ALL namespaces (admin-only, paginated)
                    limit = max(1, min(int(qs.get("limit", [50])[0]), 200))
                    offset = max(0, int(qs.get("offset", [0])[0]))
                    fns = (qs.get("namespace", [""])[0]).strip() or None
                    ftype = (qs.get("type", [""])[0]).strip().lower() or None
                    if ftype and ftype not in ALLOWED_MEMORY_TYPES:
                        return 400, {"error": "unknown type %r (allowed: %s)"
                                     % (ftype, " | ".join(ALLOWED_MEMORY_TYPES))}
                    return 200, {"memories": Control.memory_feed(conn, limit, offset, fns, ftype),
                                 "limit": limit, "offset": offset}
                if sub == "audit" and method == "GET":
                    # server-side pagination: limit clamped 1..1000, total is APPROXIMATE
                    # (planner stats) so the endpoint stays O(page) as the log grows
                    limit = max(1, min(int(qs.get("limit", [100])[0]), 1000))
                    offset = max(0, int(qs.get("offset", [0])[0]))
                    return 200, {"audit": Control.recent_audit(conn, limit, offset),
                                 "total": Control.audit_total(conn), "total_estimated": True,
                                 "limit": limit, "offset": offset}
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
                    from core.vault import Vault
                    return 200, {"mode": "openai" if os.environ.get("OPENAI_API_KEY") else "local",
                                 "dim": DIM, "key_present": bool(os.environ.get("OPENAI_API_KEY")),
                                 "extraction": LLM is not None,
                                 "extract_base_url": os.environ.get("MEMNOS_EXTRACT_BASE_URL"),
                                 "extract_model": EXTRACT_MODEL if LLM is not None else None,
                                 "vault_unlocked": Vault.available()}
                if sub == "secrets":
                    from core.vault import Vault, VaultLocked
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

    def _user(self, method, path, qs, body):
        """USER-scoped API (issue #20, Part A): any valid token, scoped to ITS OWN
        principal — NOT admin-gated. A principal can only see/modify its own bindings +
        hosts. Returns (code, obj) or None if `path` isn't a user route (so the caller
        falls through to the normal dispatch). Mirrors `_admin`'s shape."""
        if not (path == "/bindings" or path.startswith("/bindings/")
                or path == "/hosts" or path == "/bindings/recap" or path == "/nudges"):
            return None
        with POOL.connection() as conn:
            pid = Control.authenticate(conn, self._token())
            if pid is None:
                return 401, {"error": "unauthorized"}
            body = body or {}
            try:
                if path == "/bindings/recap" and method == "GET":
                    try:
                        days = int((qs or {}).get("days", ["7"])[0])
                    except Exception:
                        days = 7
                    return 200, {"days": days, "recap": Control.write_recap(conn, pid, days=days)}
                if path == "/bindings" and method == "GET":
                    return 200, {"bindings": Control.list_bindings(conn, pid),
                                 "hosts": Control.list_hosts(conn, pid)}
                if path == "/bindings" and method == "POST":
                    kt = str(body.get("key_type", "")).strip()
                    key = str(body.get("key", "")).strip()
                    ns = str(body.get("namespace", "")).strip()
                    host_id = (str(body.get("host_id", "")).strip() or None)
                    if kt not in ("repo", "host_repo", "host_path"):
                        return 400, {"error": "key_type must be 'repo'|'host_repo'|'host_path'"}
                    if not key or not ns or len(ns) > 200:
                        return 400, {"error": "key and namespace required (namespace <=200 chars)"}
                    if kt in ("host_repo", "host_path") and not host_id:
                        return 400, {"error": "host_id required for host-scoped binding"}
                    row = Control.upsert_binding(conn, pid, kt, key, ns, host_id)
                    return 200, {"binding": row}
                if path.startswith("/bindings/") and method == "DELETE":
                    try:
                        bid = int(path[len("/bindings/"):])
                    except ValueError:
                        return 400, {"error": "binding id (int) required"}
                    if not Control.delete_binding(conn, pid, bid):
                        return 404, {"error": "binding not found"}   # also not-theirs -> 404
                    return 200, {"ok": True}
                if path == "/nudges" and method == "GET":
                    # deferred suggest-on-mismatch (issue #20, Part B3): return + mark
                    # delivered (at-most-once display). The SessionStart hook surfaces these.
                    return 200, {"nudges": Control.take_pending_nudges(conn, pid)}
                if path == "/hosts" and method == "GET":
                    return 200, {"hosts": Control.list_hosts(conn, pid)}
                if path == "/hosts" and method == "POST":
                    mid = str(body.get("machine_id", "")).strip()
                    fname = (str(body.get("friendly_name", "")).strip() or None)
                    if not mid:
                        return 400, {"error": "machine_id required"}
                    return 200, {"host": Control.upsert_host(conn, pid, mid, fname)}
            except Exception as e:
                traceback.print_exc()
                return 500, {"error": type(e).__name__, "msg": str(e)[:200]}
            return 404, {"error": "unknown user route"}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/mcp":
            return self._forward_mcp("GET")
        # --- management console (zero-build static UI) ---
        if u.path in ("/admin", "/admin/"):
            return self._send_static("index.html")
        if u.path.startswith("/admin/api/"):
            code, obj = self._admin("GET", u.path[len("/admin/api/"):], parse_qs(u.query), None)
            return self._send(code, obj)
        if u.path.startswith("/admin/"):
            return self._send_static(u.path[len("/admin/"):])
        r = self._user("GET", u.path, parse_qs(u.query), None)
        if r is not None:
            return self._send(*r)
        if self.path == "/healthz":
            budget = {}
            if BUDGET_DAILY_USD is not None or BUDGET_MONTHLY_USD is not None:
                try:
                    with POOL.connection() as conn:
                        bs = Control.budget_status(conn, BUDGET_DAILY_USD, BUDGET_MONTHLY_USD)
                    budget = {"daily_ok": bs["daily_ok"], "monthly_ok": bs["monthly_ok"]}
                except Exception:
                    budget = {}
            resp = {"ok": True}
            if budget:
                resp["budget"] = budget
            return self._send(200, resp)
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
        if u.path == "/mcp":
            return self._forward_mcp("DELETE")
        if u.path.startswith("/admin/api/"):
            code, obj = self._admin("DELETE", u.path[len("/admin/api/"):], parse_qs(u.query), None)
            return self._send(code, obj)
        r = self._user("DELETE", u.path, parse_qs(u.query), None)
        if r is not None:
            return self._send(*r)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/mcp":
            return self._forward_mcp("POST")
        # --- management console admin API ---
        if u.path.startswith("/admin/api/"):
            body = self._read_body()
            if body is None:
                return self._send(400, {"error": "invalid json"})
            code, obj = self._admin("POST", u.path[len("/admin/api/"):], parse_qs(u.query), body)
            return self._send(code, obj)

        # --- user-scoped binding/host registry (no namespace in body) ---
        if u.path == "/bindings" or u.path == "/hosts":
            body = self._read_body()
            if body is None:
                return self._send(400, {"error": "invalid json"})
            return self._send(*self._user("POST", u.path, parse_qs(u.query), body))

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

        if self.path in ("/remember", "/memory/write"):
            # dedicated phased path: a pool connection must NEVER be held across the
            # embedding or LLM extraction calls — that starves the pool under concurrent
            # sessions (field: 30s admin queueing behind in-flight hook writes)
            if self.path == "/memory/write":   # alias accepts text OR content
                req["text"] = str(req.get("text", "") or req.get("content", "")).strip()
            return self._remember_phased(req, ns, token, t0,
                                         action=self.path.lstrip("/"))
        if self.path in PHASED_OPS:
            # endpoints with slow non-DB work (query embedding, cross-encoder rerank,
            # per-entity dossier LLM calls, chunk extraction, file parsing): same phased
            # discipline — short conn for auth/reads/writes, NO conn during model I/O.
            return self._phased(req, ns, token, t0)

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
                    body = {"error": "forbidden for namespace"}
                    if write:
                        try:
                            wns = Control.writable_namespaces(conn, principal)
                            if wns:
                                body["writable_namespaces"] = wns
                                body["hint"] = ("switch namespace with /memnos ns=<namespace> "
                                                "or set MEMNOS_NS env var")
                        except Exception:
                            pass
                    return self._send(403, body)

                if self.path == "/feedback":   # the true quality signal: was recall helpful?
                    Control.record_feedback(conn, principal, ns, str(req.get("query", ""))[:1000],
                                            bool(req.get("helpful", False)), (str(req.get("note", "")) or None))
                    Control.audit(conn, principal, "feedback", ns, True,
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=200)
                    return self._send(200, {"ok": True})

                store = BrainStore(conn=conn)
                usage = _UsageAcc()        # captures extraction/consolidation LLM tokens+cost
                mem = MemnosMemory(store, EMBED, dim=DIM, llm=LLM, extract_model=EXTRACT_MODEL, on_usage=usage)
                cost0 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                action = self.path.lstrip("/")
                try:
                    # --- episodic decay (pure SQL; segment/recall are PHASED above) ---
                    if self.path == "/episode/decay":
                        out = {"updated": store.decay_episodes(mem.schema, ns,
                                                               half_life_days=float(req.get("half_life_days", 30)))}
                    elif self.path == "/episode":
                        try:
                            eid = int(req.get("id"))
                        except (TypeError, ValueError):
                            return self._send(400, {"error": "id (int) required"})
                        res = store.get_episode(mem.schema, ns, eid)
                        if res is None:
                            return self._send(404, {"error": "episode not found"})
                        store.touch_episodes(mem.schema, [eid])
                        out = res
                    # --- memory CRUD parity (write/search/context are PHASED above) ---
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
                    elif self.path == "/provenance":       # evidence chain for a fact
                        try:
                            sid = int(req.get("id"))
                        except (TypeError, ValueError):
                            return self._send(400, {"error": "id (int) required"})
                        res = store.provenance_of(mem.schema, ns, sid)
                        if res is None:
                            return self._send(404, {"error": "fact not found in namespace"})
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
                    # --- copy / move memories between namespaces ---
                    elif self.path == "/namespace/copy":   # namespace = DESTINATION (write-authed above)
                        src = str(req.get("src", "")).strip()
                        if not src:
                            return self._send(400, {"error": "src (source namespace) required"})
                        if not Control.authorize(conn, principal, src, write=False):  # need READ on source
                            return self._send(403, {"error": f"forbidden: read on source namespace {src}"})
                        mode = "move" if str(req.get("mode", "copy")).lower() == "move" else "copy"
                        if src == ns:
                            return self._send(400, {"error": "src and destination must differ"})
                        out = store.migrate_namespace(mem.schema, src, ns, mode=mode,
                                                      like=(str(req["like"]) if req.get("like") else None))
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
                    # --- agent coordination leases (issue #26) ---
                    elif self.path == "/lease/acquire":
                        key = str(req.get("key", "")).strip()
                        holder = str(req.get("holder_id", "")).strip()
                        ttl = max(1, int(req.get("ttl_seconds", 1200)))
                        if not key or not holder:
                            return self._send(400, {"error": "key and holder_id required"})
                        out = Control.lease_acquire(conn, ns, key, holder, principal, ttl)
                        if out.get("granted"):
                            store.insert_raw_turn(
                                mem.schema, ns, "", "memnos:lease",
                                json.dumps({"event": "acquired", "key": key, "holder_id": holder,
                                            "expires_at": out["expires_at"]}),
                                datetime.now(timezone.utc), None)
                    elif self.path == "/lease/heartbeat":
                        key = str(req.get("key", "")).strip()
                        holder = str(req.get("holder_id", "")).strip()
                        ttl = max(1, int(req.get("ttl_seconds", 1200)))
                        if not key or not holder:
                            return self._send(400, {"error": "key and holder_id required"})
                        out = Control.lease_heartbeat(conn, ns, key, holder, ttl)
                    elif self.path == "/lease/release":
                        key = str(req.get("key", "")).strip()
                        holder = str(req.get("holder_id", "")).strip()
                        if not key or not holder:
                            return self._send(400, {"error": "key and holder_id required"})
                        out = Control.lease_release(conn, ns, key, holder)
                        if out.get("released"):
                            store.insert_raw_turn(
                                mem.schema, ns, "", "memnos:lease",
                                json.dumps({"event": "released", "key": key, "holder_id": holder}),
                                datetime.now(timezone.utc), None)
                    elif self.path == "/lease/who_holds":
                        key = str(req.get("key", "")).strip()
                        if not key:
                            return self._send(400, {"error": "key required"})
                        out = Control.lease_who_holds(conn, ns, key)
                    elif self.path == "/lease/list":
                        out = {"leases": Control.lease_list(conn, ns)}
                    # --- corpus ingestion + checks (Batch 4) ---
                    elif self.path == "/corpus/ingest":    # parse doc constraints -> facts
                        name = str(req.get("name", "")).strip()
                        text = str(req.get("text", ""))
                        if not name or not text.strip():
                            return self._send(400, {"error": "name and text required"})
                        pname = (Control.principal_info(conn, principal) or {}).get("name")
                        ids = store.ingest_constraints(mem.schema, ns, name, text, author=pname)
                        Control.corpus_record(conn, ns, name, str(req.get("kind", "doc")),
                                              (str(req.get("git_sha")) or None) if req.get("git_sha") else None,
                                              len(ids))
                        out = {"source": name, "constraints": len(ids), "ids": ids}
                    elif self.path == "/corpus/check":      # constraints relevant to a snippet
                        snippet = str(req.get("snippet", "") or req.get("code", ""))
                        if not snippet.strip():
                            return self._send(400, {"error": "snippet/code required"})
                        out = {"constraints": store.corpus_check(mem.schema, ns, snippet)}
                    elif self.path == "/entity/dossier":   # get stored dossier for an entity
                        entity_name = str(req.get("entity", "")).strip()
                        if not entity_name:
                            return self._send(400, {"error": "entity name required"})
                        res = store.get_entity_dossier(mem.schema, ns, entity_name)
                        if res is None:
                            return self._send(404, {"error": "no dossier found for this entity"})
                        out = {
                            "entity": res["name"],
                            "dossier": res["dossier_text"],
                            "generated_at": res["generated_at"].isoformat() if res.get("generated_at") else None,
                            "model_used": res.get("model_used"),
                        }
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
        except (PoolTimeout, OperationalError) as e:
            # fail FAST and clearly when the database is down — never leave clients hanging
            print(f"[memnos] DB unreachable ({type(e).__name__}): {e}", flush=True)
            return self._send(503, {"error": "database unreachable — is Postgres running?"})
        except Exception:
            traceback.print_exc()              # pool/connection-level failure
            return self._send(500, {"error": "internal error"})

    def log_message(self, *a):
        pass

    def _auth_short(self, ns, token, *, write, action):
        """Short-lived auth+ACL phase on its OWN pool connection (released before any
        slow work). Returns (principal, pname, None) or (None, None, (code, obj)) to
        send. `pname` = the authenticated principal's NAME — the only source for
        author attribution (never the request body)."""
        with POOL.connection() as conn:
            principal = Control.authenticate(conn, token)
            if principal is None:
                return None, None, (401, {"error": "unauthorized"})
            if not Control.authorize(conn, principal, ns, write=write):
                Control.audit(conn, principal, action, ns, False, {"reason": "forbidden"})
                body = {"error": "forbidden for namespace"}
                if write:
                    try:
                        wns = Control.writable_namespaces(conn, principal)
                        if wns:
                            body["writable_namespaces"] = wns
                            body["hint"] = ("switch namespace with /memnos ns=<namespace> "
                                            "or set MEMNOS_NS env var")
                    except Exception:
                        pass
                return None, None, (403, body)
            info = Control.principal_info(conn, principal)
        return principal, (info or {}).get("name"), None

    def _remember_phased(self, req, ns, token, t0, action="remember"):
        """/remember (and its /memory/write alias) in phases — the 'LLM at ingest only,
        NEVER on the request path while holding resources' rule from the architecture:
          P0 (conn, ~ms):  auth + ACL
          P1a (NO conn):   redact + EMBED the turn — network I/O in 1536 mode
          P1b (conn, ~ms): store the verbatim raw turn
          P2 (NO conn):    LLM fact extraction — pure model I/O
          P3 (conn, ~ms):  supersession + fact writes + usage + audit
        Default stays SYNCHRONOUS (same response contract). {"async": true} defers
        P2+P3 to the background ingest workers and returns immediately after P1 —
        used by the Claude Code Stop hook, which never reads the fact count."""
        text = str(req.get("text", "")).strip()
        if not text or len(text) > 20000:
            return self._send(400, {"error": "text required (<=20000 chars)"})
        ok, mtype = _memory_type(req)              # typed memory (decision|incident|...)
        if not ok:
            return self._send(400, mtype)
        run_async = bool(req.get("async"))
        usage = _UsageAcc()
        cost0 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
        principal = None                               # bound for the failure-audit path
        try:
            principal, pname, err = self._auth_short(ns, token, write=True, action=action)  # P0
            if err:
                return self._send(*err)
            from core.redact import redact as _redact
            rtext0, _n = _redact(text)                            # P1a — CPU
            vec = EMBED(rtext0)                                   # P1a — network, NO conn
            with POOL.connection() as conn:                       # P1b — short DB write
                # author = AUTHENTICATED principal's name (token-derived; body ignored)
                mem = MemnosMemory(BrainStore(conn=conn), EMBED, dim=DIM, llm=LLM,
                                   extract_model=EXTRACT_MODEL, on_usage=usage, author=pname,
                                   extract_fn=EXTRACT_FN)
                tid, rtext, obs = mem.remember_turn(ns, rtext0, speaker=req.get("speaker"),
                                                    session_id=req.get("session_id"), vec=vec,
                                                    memory_type=mtype)
            # conn is back in the pool. `mem` is reused ONLY for extract_facts below,
            # which never touches its store.
            if not _have_extraction():                            # local mode: no extraction
                with POOL.connection() as conn:
                    Control.audit(conn, principal, action, ns, True,
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=200)
                    # suggest-on-mismatch (issue #20, Part B) STILL fires in local mode —
                    # no LLM facts, so the advisory runs off raw-turn NER alone (the same
                    # extract_entities signal _write_suggestion uses). Advisory only: the
                    # write already landed in `ns`; this never reroutes.
                    suggestion = _write_suggestion(conn, principal, ns, [], rtext)
                _DELIVER_EVENT.set()
                out = {"turn_id": tid, "facts": 0, "superseded": 0, "namespace": ns}
                if suggestion:
                    out["suggestion"] = suggestion
                return self._send(200, out)
            if run_async:
                try:
                    _INGEST_Q.put_nowait((ns, rtext, obs, tid, principal, mem, cost0, t0, mtype))
                    return self._send(200, {"turn_id": tid, "facts": None,
                                            "extraction": "queued", "namespace": ns})
                except Exception:                                  # queue full → fall through to sync
                    pass
            facts = mem.extract_facts(rtext, obs, memory_type=mtype)  # P2 — NO conn held
            if hasattr(EMBED, "prime") and facts:                 # batch-embed fact statements, NO conn
                EMBED.prime([f["statement"] for f in facts])
            with POOL.connection() as conn:                       # P3
                mem3 = MemnosMemory(BrainStore(conn=conn), EMBED, dim=DIM, llm=LLM,
                                    extract_model=EXTRACT_MODEL, on_usage=usage, author=pname,
                                    extract_fn=EXTRACT_FN)
                # suggest-on-mismatch (issue #20, Part B): advisory only — NEVER reroutes.
                # MUST run BEFORE write_facts persists this turn's entities into `ns`,
                # else `ns` self-pollutes with its own just-extracted entities and the
                # "other namespace must dominate AND exceed the write ns" check can never
                # fire (the write ns trivially dominates its own write). We judge against
                # the PRE-write entity state of every namespace.
                suggestion = _write_suggestion(conn, principal, ns, facts, rtext)
                nf, nsup = mem3.write_facts(ns, facts, obs, tid, memory_type=mtype)
                cost1 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                Control.record_usage(conn, principal, ns, action, mem3.extract_model,
                                     usage.tin, usage.tout, round((cost1 - cost0) + usage.cost, 6))
                Control.audit(conn, principal, action, ns, True,
                              latency_ms=int((time.perf_counter() - t0) * 1000), status=200)
            _DELIVER_EVENT.set()
            out = {"turn_id": tid, "facts": nf, "superseded": nsup, "namespace": ns}
            if suggestion:
                out["suggestion"] = suggestion
            return self._send(200, out)
        except (PoolTimeout, OperationalError) as e:
            print(f"[memnos] DB unreachable ({type(e).__name__}): {e}", flush=True)
            return self._send(503, {"error": "database unreachable — is Postgres running?"})
        except Exception as op_err:
            traceback.print_exc()
            # audit-parity with the pre-phased handler: a failed remember MUST leave an
            # audit row with what broke (actionable via `memnos_admin.py errors`)
            try:
                with POOL.connection() as conn:
                    Control.audit(conn, principal, action, ns, False,
                                  {"error": type(op_err).__name__, "msg": str(op_err)[:300]},
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=500)
            except Exception:
                pass                                   # DB down — the 500 still reaches the client
            return self._send(500, {"error": "internal error"})

    def _phased(self, req, ns, token, t0):
        """Generic phased dispatcher for PHASED_OPS: P0 short conn (auth+ACL) → op with
        the phased discipline (model I/O with NO conn; DB via short-lived conns) →
        P-final short conn (usage + audit). Response contracts identical to the old
        in-block handlers."""
        action = self.path.lstrip("/")
        usage = _UsageAcc()
        cost0 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
        self._audit_detail = None      # per-stage timings (recall ops set this)
        try:
            principal, pname, err = self._auth_short(ns, token, write=self.path in WRITE_OPS,
                                                     action=action)
            if err:
                return self._send(*err)
            # store=None: DB phases attach a short-lived BrainStore per pool conn.
            # author = authenticated principal's name (stamped on every write).
            mem = MemnosMemory(None, EMBED, dim=DIM, llm=LLM, extract_model=EXTRACT_MODEL, on_usage=usage, author=pname)
            try:
                code, out = self._phased_op(mem, req, ns, principal, t0)
            except Exception as op_err:
                with POOL.connection() as conn:
                    Control.audit(conn, principal, action, ns, False,
                                  {"error": type(op_err).__name__, "msg": str(op_err)[:300]},
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=500)
                traceback.print_exc()
                return self._send(500, {"error": "internal error"})
            if code != 200:
                return self._send(code, out)
            with POOL.connection() as conn:                       # usage + audit (short)
                cost1 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                Control.record_usage(conn, principal, ns, action, mem.extract_model,
                                     usage.tin, usage.tout, round((cost1 - cost0) + usage.cost, 6))
                rcount = len(out.get("memories", [])) if isinstance(out, dict) and "memories" in out else None
                # detail = per-stage timings for recall ops ({embed_ms, sql_ms,
                # staleness_ms, rerank_ms, total_ms}) — regressions stay diagnosable
                # from the ledger alone (issue #12)
                Control.audit(conn, principal, action, ns, True, self._audit_detail,
                              latency_ms=int((time.perf_counter() - t0) * 1000),
                              result_count=rcount, status=200)
            if self.path in WRITE_OPS:
                _DELIVER_EVENT.set()
            return self._send(200, out)
        except (PoolTimeout, OperationalError) as e:
            print(f"[memnos] DB unreachable ({type(e).__name__}): {e}", flush=True)
            return self._send(503, {"error": "database unreachable — is Postgres running?"})
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": "internal error"})

    @staticmethod
    def _author_filter(rows, req):
        """Optional /recall `author` filter — match against the SERVER-stamped
        author_principal (rows without an author never match a filter)."""
        author = str(req.get("author", "")).strip()
        if not author:
            return rows
        return [r for r in rows if r.get("author") == author]

    def _phased_op(self, mem, req, ns, principal, t0):
        """One PHASED_OPS endpoint body. Returns (code, out). Pool connections are
        acquired ONLY around pure-DB sections; embeddings/LLM/rerank run with none."""
        path = self.path
        if path in ("/recall", "/memory/search", "/recall_v2", "/memory/context"):
            q = str(req.get("query", "")).strip()
            if not q:
                return 400, {"error": "query required"}
            # issue #15: a long recall query must return 200, not crash the FTS tsquery
            # parser. The embedder truncates and the FTS arm is token-clamped (store.
            # fts_clamp), so a long query is SAFE — clamp it to a generous ceiling here
            # rather than 400-rejecting it. MEMNOS_QUERY_MAX_CHARS tunes the ceiling.
            if len(q) > _QUERY_MAX_CHARS:
                q = q[:_QUERY_MAX_CHARS]
            ok, mtype = _memory_type(req)          # optional `type` result filter
            if not ok:
                return 400, mtype
            try:                                   # pinned-constraint cap (0 disables)
                pin_cap = max(0, min(int(req.get("constraint_cap", 10)), 50))
            except (TypeError, ValueError):
                return 400, {"error": "constraint_cap must be an integer"}
            # DEADLINE-AWARE RECALL (issue #12): optional client budget. At expiry the
            # server returns best-available results (un-reranked, staleness pass
            # skipped) flagged degraded:true — never a client-side timeout.
            deadline = None
            if req.get("deadline_ms") is not None:
                try:
                    dl_ms = int(req["deadline_ms"])
                except (TypeError, ValueError):
                    return 400, {"error": "deadline_ms must be an integer"}
                if dl_ms > 0:
                    deadline = t0 + dl_ms / 1000.0
            # engine's CANONICAL (benchmarked) defaults; only override when the client
            # explicitly supplies a value — no server-side drift.
            rkw = {}
            if "raw_quota" in req: rkw["raw_quota"] = int(req["raw_quota"])
            if "fact_quota" in req: rkw["fact_quota"] = int(req["fact_quota"])
            # issue #17: optional hard SUBJECT scope — recall returns only the named
            # entity's facts (single-namespace path only; ignored for wide recall).
            subject_scope = (str(req["subject"]).strip() if req.get("subject") else None)
            ckw = {"max_chars": int(req["max_chars"])} if "max_chars" in req else {}
            timings = {}
            # QUERY EMBED: short-TTL cache hit skips the round-trip; a miss runs on the
            # embed executor OVERLAPPED with DB phase A below (the non-vector arms) —
            # the vector arms fire when the embedding arrives. No conn is ever held
            # across the network call (each DB phase uses its own short-lived conn).
            model_key = getattr(EMBED, "model", "local-384")
            qv = _QUERY_CACHE.get(q, model_key)
            fut = None
            if qv is None:
                def _embed_timed():
                    te = time.perf_counter()
                    v = mem.embed(query_clamp(q))                 # #15 follow-up: bound long query
                    timings["embed_ms"] = (time.perf_counter() - te) * 1000.0
                    return v
                fut = _EMBED_EXEC.submit(_embed_timed)
            else:
                timings["embed_ms"] = 0.0                         # cache hit
            wide = path == "/recall" and str(req.get("scope", "")).lower() in ("all", "wide")
            grounded, skipped = [], []
            other_readable = []    # issue #16: namespaces the principal may read beyond ns
            # issue #41 fix C: readable_namespaces() fan-out failures collect here (wide
            # scope only — the narrow-path other_readable lookup below is a scope HINT,
            # not a results source, so its own failure never marks the recall degraded).
            wide_degraded_reasons = []
            pre = None
            t_a = time.perf_counter()
            with POOL.connection() as conn:        # DB phase A — needs NO embedding
                mem.store = BrainStore(conn=conn)
                if wide:
                    # WIDEN across every namespace this key may read (ACL-bounded).
                    # issue #41 fix C: readable_namespaces() is itself a live DB call (it
                    # drives WHICH namespaces get searched) and can fail the same way any
                    # other recall arm can. A RECALL_ARM_FAILURES-class error falls back
                    # to just [ns] — the caller is already known to hold a read grant on
                    # ns (checked in _auth_short before this handler ever runs), so that
                    # fallback is always safe — and flags the recall degraded, since the
                    # search scope was cut from "everything readable" down to one
                    # namespace, a real reduction in coverage, not just a hint.
                    try:
                        nss = Control.readable_namespaces(conn, principal)
                    except RECALL_ARM_FAILURES as e:
                        print(f"[memnos] recall degraded: readable_namespaces fan-out "
                              f"failed for principal={principal} ({type(e).__name__}): {e} "
                              f"— falling back to primary namespace only", flush=True)
                        nss = [ns]
                        wide_degraded_reasons.append(
                            {"namespace": ns, "arm": "readable_namespaces",
                             "error": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None)})
                    # scope hint: other readable = all readable minus the query ns, cap 10
                    other_readable = [n for n in nss if n != ns][:10]
                    pin_nss = [ns]                 # pin the TARGET namespace's constraints
                else:
                    # GROUNDED RECALL: fan out to LINKED knowledge namespaces, but only
                    # those the CALLER may read (link = policy, grant = permission —
                    # both required). Skipped links are reported, never silent.
                    links = Control.linked_namespaces(conn, ns)
                    for kns in links:
                        (grounded if Control.authorize(conn, principal, kns, write=False)
                         else skipped).append(kns)
                    # issue #16: scope observability — other namespaces this principal can
                    # read. issue #41 fix C: this is a DISPLAY HINT (scope.hint /
                    # other_readable_namespaces), not a source of results, so a failure
                    # here degrades to [] and is logged but never flips degraded=true —
                    # that flag's job is "results are incomplete," not "a hint is missing."
                    if path == "/recall":
                        try:
                            other_readable = Control.readable_namespaces(
                                conn, principal, exclude=ns, limit=10)
                        except RECALL_ARM_FAILURES as e:
                            print(f"[memnos] other_readable_namespaces hint degraded for "
                                  f"principal={principal} ({type(e).__name__}): {e}", flush=True)
                            other_readable = []
                    pre = mem.recall_prefetch(ns, q)   # timeline/entity arms, no qv
                    pin_nss = [ns] + grounded
                # PINNED CONSTRAINT INJECTION: type='constraint' memories are ALWAYS in
                # the output, regardless of query similarity (cap via constraint_cap).
                # issue #41 fix C: pin_cap defaults to 10 (see above), not 0, so this is a
                # LIVE query against {schema}.semantic/raw_turns/episodic — same tables,
                # same connection, same statement_timeout as every other recall arm — on
                # the DEFAULT request path, before recall_fetch's own (correctly-guarded)
                # arms even run. An earlier pass left this uncaught on the theory that
                # silently dropping an always-present pin was the wrong failure mode; that
                # reasoning missed that "uncaught" here doesn't mean "the pin is dropped",
                # it means the exception propagates out of this whole handler and the
                # ENTIRE recall 500s — defeating #41's own acceptance criterion (recall
                # must never hard-fail on a live arm failure) for any client that doesn't
                # know to send constraint_cap:0. Guarded the same way every other arm in
                # this file is: degrade to an empty pin list and record the failure.
                pin_reasons = (wide_degraded_reasons if wide
                               else pre.setdefault("_degraded_reasons", []))
                try:
                    pins = mem.store.pinned_constraints(mem.schema, pin_nss, cap=pin_cap)
                except RECALL_ARM_FAILURES as e:
                    pins = []
                    print(f"[memnos] pinned_constraints degraded for ns={pin_nss} "
                          f"({type(e).__name__}): {e}", flush=True)
                    pin_reasons.append({"namespace": ns, "arm": "pinned_constraints",
                                        "error": type(e).__name__,
                                        "sqlstate": getattr(e, "sqlstate", None)})
                    if not wide:
                        # set directly rather than relying on recall_fetch's own
                        # `if reasons: b["_degraded"] = True` epilogue to notice this
                        # list is non-empty — keeps this failure self-sufficient even
                        # if that epilogue's control flow ever changes.
                        pre["_degraded"] = True
                mem.store = None
            timings["sql_ms"] = (time.perf_counter() - t_a) * 1000.0
            if fut is not None:
                # TODO(issue #41 follow-up, embed/rerank-phase degrade): fut.result() —
                # and the rerank call below — sit entirely outside RECALL_ARM_FAILURES
                # coverage. A live failure here (e.g. a network reset mid-embed) still
                # propagates and 500s the whole recall, same symptom #41 fixed for the
                # four DB arms. Out of #41's stated scope (raw/semantic/semantic_temporal/
                # timeline + wide fan-out) — deserves its own scoped issue/PR rather than
                # folding it into this one, same call PR #43 made deferring this fix.
                qv = fut.result()                  # join the embed — NO conn held
                _QUERY_CACHE.put(q, model_key, qv)
            with POOL.connection() as conn:        # DB phase B — vector + FTS arms
                mem.store = BrainStore(conn=conn)
                if wide:
                    # issue #41 fix C: wide_degraded_reasons already may carry the phase-A
                    # readable_namespaces failure (if any); recall_wide_fetch appends any
                    # per-namespace arm failures from THIS phase to the same list.
                    raw_c, sem_c = mem.recall_wide_fetch(nss, q, qv=qv, timings=timings,
                                                         deadline=deadline,
                                                         degraded_reasons=wide_degraded_reasons)
                else:
                    bundle = mem.recall_fetch(ns, q, qv=qv, extra_namespaces=grounded,
                                              pre=pre, timings=timings, deadline=deadline)
                mem.store = None
            timings.setdefault("staleness_ms", 0.0)   # wide+empty-namespace edge
            pin_rows = []
            for p in pins:
                row = {"content": p["content"], "kind": p["kind"], "type": "constraint",
                       "pinned": True}
                if p.get("author"):
                    row["author"] = p["author"]
                if p["namespace"] != ns:           # grounded source — tagged like ranked rows
                    row["namespace"] = p["namespace"]
                pin_rows.append(row)
            # CPU rerank phase — NO conn (ranking identical to pre-split recall).
            # Past-deadline: skip the cross-encoder, keep retrieval (RRF) order.
            # issue #41 fix C: degraded_reasons — narrow path reads them off the bundle
            # (recall_fetch/recall_prefetch populate bundle['_degraded_reasons']); wide
            # path already has them in wide_degraded_reasons (readable_namespaces fallback
            # + recall_wide_fetch's per-namespace failures). `bundle` is only bound on the
            # narrow path, so this must stay inside the `if wide` guard, same as the
            # original single-line version did via `(not wide) and ...` short-circuiting.
            if wide:
                degraded_reasons = wide_degraded_reasons
                degraded = bool(degraded_reasons)
            else:
                degraded = bool(bundle.pop("_degraded", False))
                degraded_reasons = bundle.pop("_degraded_reasons", [])
            degraded_reasons = degraded_reasons[:_DEGRADED_REASONS_CAP]
            # DEGRADED-WHILE-WARMING (follow-up to #12): if the reranker isn't ready yet
            # (background prewarm still loading the model), serve RRF-fused results NOW —
            # same degraded contract the deadline path uses — instead of blocking on the
            # heavy load. This is the real cold-start fix: first call is fast on EVERY box.
            warming = not brain_rerank.is_ready()
            past_deadline = deadline is not None and time.perf_counter() >= deadline
            use_rerank = (not warming) and (not past_deadline)
            degraded = degraded or not use_rerank
            t_rr = time.perf_counter()
            if wide:
                rows = mem.recall_wide_rank(q, raw_c, sem_c, use_rerank=use_rerank, **rkw)
            else:
                rows = mem.recall_rank(q, bundle, use_rerank=use_rerank,
                                       subject=subject_scope, **rkw)
            timings["rerank_ms"] = (time.perf_counter() - t_rr) * 1000.0
            rows = self._author_filter(rows, req)
            if mtype:                              # type filter (pins are exempt — always on)
                rows = [r for r in rows if r.get("type") == mtype]
            if pin_rows:                           # constraints FIRST; add, don't replace
                pinned_contents = {p["content"] for p in pin_rows}
                rows = pin_rows + [r for r in rows if r["content"] not in pinned_contents]
            if wide:
                out = {"memories": rows,
                       "context": mem.render_context(rows, viewer=mem.author, query=q, **ckw),
                       "namespaces_searched": nss}
            else:
                if path == "/recall":
                    out = {"memories": rows,
                           "context": mem.render_context(rows, viewer=mem.author, query=q, **ckw)}
                elif path == "/memory/context":
                    out = {"context": mem.render_context(rows, viewer=mem.author, query=q, **ckw)}
                else:                                             # /memory/search, /recall_v2
                    out = {"memories": rows}
                if grounded or skipped:    # keys only appear when links exist (no drift)
                    out["grounded_in"] = grounded
                    out["links_skipped"] = skipped
            # issue #16: scope observability — always attach for /recall (both wide + narrow)
            if path == "/recall":
                ns_searched = nss if wide else [ns]
                scope = {
                    "namespaces_searched": ns_searched,
                    "facts_found": len(rows),
                    "query_namespace": ns,
                    "other_readable_namespaces": other_readable,
                }
                if other_readable:
                    scope["hint"] = ("results scoped to " + ns
                                     + " -- token can also read: "
                                     + ", ".join(other_readable))
                out["scope"] = scope
            if degraded:                   # deadline hit / warming / arm failure — flagged
                out["degraded"] = True
                # issue #41 fix C: only present when a real arm failed (deadline/warming
                # degrades leave degraded_reasons empty) — keys only appear when there's
                # something to say, same "no drift" convention as grounded_in/links_skipped
                # above.
                if degraded_reasons:
                    out["degraded_reasons"] = degraded_reasons
            timings["total_ms"] = (time.perf_counter() - t0) * 1000.0
            detail = {k: round(v, 1) for k, v in timings.items()}
            if degraded:
                detail["degraded"] = True
                if degraded_reasons:
                    detail["degraded_reasons"] = degraded_reasons
            # rerank calibration (follow-up to #12): record what THIS box calibrated to
            # so an operator can see the derived cap / measured ms-per-pair per recall.
            cal = brain_rerank.calibration()
            detail["effective_cap"] = cal["effective_cap"]
            if cal["measured_ms_per_pair"] is not None:
                detail["measured_ms_per_pair"] = cal["measured_ms_per_pair"]
            self._audit_detail = detail    # → audit_log.detail (per-stage ledger)
            # issue #15: hand the recall's transient heap (candidate strings, fused rows,
            # ONNX forward-pass working set) back to the OS so phys_footprint/RSS stays
            # flat instead of ratcheting up and plateauing high. Rate-limited internally,
            # so a recall BURST doesn't pay it per request; no-op where unsupported.
            memrelief.release()
            return 200, out

        if path == "/consolidate":
            # read (conn) -> per-entity dossier LLM + embeddings (NO conn) -> write (conn)
            infer_flag = os.environ.get("MEMNOS_INFER_ON_SLEEP") == "1" and mem.llm is not None
            result = mem.consolidate(ns, conn_factory=POOL.connection, infer=infer_flag)
            # ENTITY DOSSIER GENERATION (issue #23): only when MEMNOS_ENTITY_DOSSIERS=1
            if os.environ.get("MEMNOS_ENTITY_DOSSIERS") == "1" and mem.llm is not None:
                from core.service import generate_entity_dossier
                with POOL.connection() as conn:
                    candidates = Control.entity_dossier_candidates(
                        conn, mem.schema, ns, min_mentions=3)
                n_dossiers = 0
                for cand in candidates[:5]:
                    entity_id = cand["entity_id"]
                    entity_name = cand["name"]
                    # gather facts mentioning this entity (semantic only)
                    with POOL.connection() as conn:
                        store = BrainStore(conn=conn)
                        ent = store.get_entity(mem.schema, ns, entity_name, fact_limit=30)
                    facts = [r["content"] for r in (ent or {}).get("facts", []) if r.get("content")]
                    if not facts:
                        continue
                    text = generate_entity_dossier(entity_name, facts, mem.llm, mem.extract_model)
                    if text:
                        with POOL.connection() as conn:
                            store = BrainStore(conn=conn)
                            store.store_entity_dossier(mem.schema, entity_id, ns, text,
                                                       model_used=mem.extract_model)
                        n_dossiers += 1
                result["dossiers_generated"] = n_dossiers
            return 200, result

        if path == "/episode/segment":
            return 200, mem.segment_episodes(ns, gap_minutes=int(req.get("gap_minutes", 30)),
                                             conn_factory=POOL.connection)

        if path == "/episode/recall":
            q = str(req.get("query", "")).strip()
            if not q:
                return 400, {"error": "query required"}
            if len(q) > _QUERY_MAX_CHARS:                        # issue #15: clamp, don't reject
                q = q[:_QUERY_MAX_CHARS]
            qv = mem.embed(query_clamp(q))                        # #15 follow-up: bound long query
            with POOL.connection() as conn:
                store = BrainStore(conn=conn)
                rows = store.search_episodic(mem.schema, ns, qv, q, k=int(req.get("k", 8)))
                store.touch_episodes(mem.schema, [r["id"] for r in rows])   # access signal for decay
            return 200, {"episodes": rows}

        if path == "/reconcile":
            stmt = str(req.get("statement", "")).strip()
            if not stmt or len(stmt) > 4000:
                return 400, {"error": "statement required (<=4000 chars)"}
            qv = mem.embed(stmt)                                  # network — NO conn
            with POOL.connection() as conn:
                out = BrainStore(conn=conn).reconcile(
                    mem.schema, ns, stmt, qv,
                    subject=(str(req["subject"]).strip() if req.get("subject") else None),
                    predicate=(str(req["predicate"]).strip() if req.get("predicate") else None))
            return 200, out

        if path == "/ingest/file":
            filename = (str(req.get("filename", "")).strip() or "upload")
            text = req.get("text")
            if text is None and req.get("content_b64"):
                try:
                    raw = base64.b64decode(req["content_b64"])
                except Exception:
                    return 400, {"error": "content_b64 not valid base64"}
                text = _extract_file_text(filename, raw)          # CPU parse — NO conn
                if text is None:
                    return 415, {"error": f"cannot extract text from {filename} "
                                 "(install pypdf/python-docx, or send extracted 'text')"}
            text = str(text or "")
            if not text.strip():
                return 400, {"error": "text or content_b64 required"}
            if len(text) > 2_000_000:
                return 413, {"error": "file too large (2MB text cap)"}
            do_extract = bool(req.get("extract", False))
            from core.redact import redact as _redact
            from datetime import datetime, timezone
            observed_at = datetime.now(timezone.utc)
            chunks = [_redact(ch)[0] for ch in _chunk_text(text, int(req.get("chunk_size", 1200)))]
            if hasattr(EMBED, "prime") and chunks:                # batch-embed, NO conn
                EMBED.prime(chunks)
            facts_per = None
            if do_extract and LLM is not None:                    # per-chunk LLM — NO conn
                facts_per = [mem.extract_facts(ch, observed_at) for ch in chunks]
                stmts = [f["statement"] for fl in facts_per for f in fl]
                if hasattr(EMBED, "prime") and stmts:
                    EMBED.prime(stmts)
            tids = []
            with POOL.connection() as conn:                       # short DB write phase
                mem.store = BrainStore(conn=conn)
                for i, ch in enumerate(chunks):
                    tid, _, _ = mem.remember_turn(ns, ch, session_id=filename,
                                                  observed_at=observed_at)
                    if facts_per:
                        mem.write_facts(ns, facts_per[i], observed_at, tid)
                    tids.append(tid)
                mem.store = None
            return 200, {"filename": filename, "chunks": len(tids), "turn_ids": tids}

        return 404, {"error": "not found"}


INGEST_WORKERS = max(1, min(int(os.environ.get("MEMNOS_INGEST_WORKERS", "2")), 8))


def _ingest_worker():
    """Background extraction for async /remember: P2 with no conn, then a short P3.
    Failures are logged — the raw turn is already durably stored either way.
    INGEST_WORKERS (env MEMNOS_INGEST_WORKERS, default 2, max 8) threads drain the queue
    so a burst of async remembers doesn't serialize behind one extraction at a time —
    each worker's P2 is pure model I/O, so they overlap cleanly."""
    while True:
        ns, rtext, obs, tid, principal, mem, cost0, t0, mtype = _INGEST_Q.get()
        try:
            usage = _UsageAcc()
            mem.on_usage = usage
            facts = mem.extract_facts(rtext, obs, memory_type=mtype)  # NO conn held
            with POOL.connection() as conn:
                # mem.author carries the AUTHENTICATED principal's name from the request
                mem3 = MemnosMemory(BrainStore(conn=conn), EMBED, dim=DIM, llm=LLM,
                                    extract_model=EXTRACT_MODEL, on_usage=usage, author=mem.author)
                # suggest-on-mismatch (issue #20, Part B3): the async response already
                # returned (extraction:queued), so the advisory can't ride it. Compute it
                # against the PRE-write entity state (same ordering rule as the sync path —
                # before write_facts self-pollutes `ns`) and persist a DEFERRED nudge the
                # next SessionStart hook surfaces. Advisory only; never reroutes the write.
                suggestion = _write_suggestion(conn, principal, ns, facts, rtext)
                if suggestion:
                    try:
                        Control.record_nudge(conn, principal, ns, suggestion["namespace"],
                                             suggestion.get("reason"), turn_id=tid)
                    except Exception:
                        pass                                       # a write never fails on the nudge
                nf, nsup = mem3.write_facts(ns, facts, obs, tid, memory_type=mtype)
                cost1 = getattr(getattr(EMBED, "meter", None), "cost", 0.0)
                Control.record_usage(conn, principal, ns, "remember", mem3.extract_model,
                                     usage.tin, usage.tout, round((cost1 - cost0) + usage.cost, 6))
                Control.audit(conn, principal, "remember", ns, True,
                              latency_ms=int((time.perf_counter() - t0) * 1000), status=200,
                              detail={"async": True, "facts": nf, "superseded": nsup})
            _DELIVER_EVENT.set()
        except Exception as e:
            print(f"[memnos] async ingest failed for turn {tid} (raw turn IS stored): "
                  f"{type(e).__name__}: {e}", flush=True)
            # audit-parity with the sync path: async extraction failures must be visible
            # in the audit log, not only in the server's stdout
            try:
                with POOL.connection() as conn:
                    Control.audit(conn, principal, "remember", ns, False,
                                  {"async": True, "turn_id": tid, "error": type(e).__name__,
                                   "msg": str(e)[:300]},
                                  latency_ms=int((time.perf_counter() - t0) * 1000), status=500)
            except Exception:
                pass


def _set_proc_title(title):
    """Show a meaningful name in ps/top/Activity Monitor instead of "python"."""
    try:
        import setproctitle
        setproctitle.setproctitle(title)
    except Exception:
        pass


def serve(port=None):
    """Boot + run the memnos server. Importable so the `memnos serve` CLI reuses it."""
    global POOL, EMBED, MCP_INTERNAL_PORT
    _set_proc_title("memnos-server")
    port = int(port or PORT)
    # BACKGROUND reranker prewarm + self-calibration + residency keep-alive (follow-up
    # to #12): prewarm now runs OFF the request path so the server accepts traffic
    # IMMEDIATELY — the synchronous version cost 13-114s at boot and recalls blocked
    # behind it (field cold first-call 49.9s). While warming, is_ready() is False and
    # recalls serve degraded (RRF order, degraded:true). Prewarm also measures this box's
    # ms-per-pair and DERIVES the rerank cap from a latency budget (a CPU laptop gets a
    # small cap, a fast box a large one) — fixing the fixed cap=100 that was 85% of every
    # warm call. See core/rerank.py — MEMNOS_RERANK=0 skips both; MEMNOS_RERANK_CAP
    # overrides the derived cap; MEMNOS_RERANK_BUDGET_MS / MIN_CAP / MAX_CAP tune it.
    brain_rerank.prewarm_background()
    brain_rerank.start_keepalive()
    # timeout=5: a request against a dead DB fails in 5s with a clear 503 — clients
    # (hooks/MCP/agents) must never sit behind a 30s pool wait.
    # max_idle=60: shrink back to min_size within a minute after bursts — a congested
    # period must not leave a wall of idle postgres backends behind (field: 22 procs)
    # statement_timeout (default 15s): one bad/oversized query can no longer wedge a
    # worker thread + pin a backend — it fails with a clear error instead.
    stmt_ms = int(os.environ.get("MEMNOS_STMT_TIMEOUT_MS", "15000"))
    POOL = ConnectionPool(DSN, min_size=2, max_size=POOL_MAX, open=True, timeout=5,
                          max_idle=60, kwargs={"autocommit": True, "row_factory": dict_row,
                                               "options": f"-c statement_timeout={stmt_ms}"})
    # wait for Postgres instead of crashing: under autostart (launchd/systemd) at login,
    # PG often isn't up yet — keep retrying with a clear, single-line heartbeat.
    while True:
        try:
            with POOL.connection() as conn:
                conn.execute("SELECT 1")
            break
        except (PoolTimeout, OperationalError) as e:
            print(f"[memnos] waiting for Postgres at {re.sub(r'://[^@]*@', '://***@', DSN)} "
                  f"({type(e).__name__}) — retrying in 5s ...", flush=True)
            time.sleep(5)
    with POOL.connection() as conn:
        Control.init(conn)                                        # control plane (incl. secrets table)
        # provider key may be a vault value-ref (secret://name) — resolve before building embedder
        k = os.environ.get("OPENAI_API_KEY", "")
        if k.startswith("secret://"):
            try:
                from core.vault import Vault
                rk = Vault.resolve(conn, k)
                if rk:
                    os.environ["OPENAI_API_KEY"] = rk
                    print("[memnos] OPENAI_API_KEY resolved from vault", flush=True)
            except Exception as e:
                print(f"[memnos] WARN: could not resolve provider key from vault: {e}", flush=True)
    EMBED = _build_embedder()
    with POOL.connection() as conn:
        # schema DDL (HNSW/GIN builds on a fresh install over existing data) may
        # legitimately exceed the request statement_timeout — exempt it
        conn.execute("SET statement_timeout = 0")
        BrainStore(conn=conn).create_schema("memnos", dim=DIM)   # memory schema
        conn.execute(f"SET statement_timeout = {stmt_ms}")
        Control.audit(conn, None, "server_start", "-", True,      # heartbeat (uptime/crash-loop signal)
                      detail={"dim": DIM})
    threading.Thread(target=_pusher_loop, name="memnos-webhook-pusher", daemon=True).start()
    for i in range(INGEST_WORKERS):
        threading.Thread(target=_ingest_worker, name=f"memnos-async-ingest-{i}", daemon=True).start()
    # issue #37 Layer 1: streamable-HTTP MCP at /mcp — additive. A failure here (bad
    # MEMNOS_MCP_INTERNAL_PORT, port already taken, mount thread died, startup timeout)
    # must NOT take down :8900 — that would break REST and the stdio adapter too, which
    # depend on nothing here. Degrade: log loudly and keep serving without /mcp.
    try:
        start_mcp_http_mount(port)
        mcp_status = "MCP HTTP transport at /mcp"
    except Exception as e:
        MCP_INTERNAL_PORT = None   # discard any partial state (e.g. a thread that died
                                   # after claiming a port) — _forward_mcp must 503, not hang
        mcp_status = "MCP HTTP transport UNAVAILABLE (see WARNING above)"
        print(f"[memnos] WARNING: MCP-HTTP-mount (issue #37 Layer 1's /mcp endpoint) FAILED "
              f"to start — continuing WITHOUT it. REST and the stdio `memnos mcp` adapter "
              f"are unaffected. {type(e).__name__}: {e}", flush=True)
    print(f"[memnos] production server on http://127.0.0.1:{port} (pool max {POOL_MAX}; "
          f"{INGEST_WORKERS} async-ingest workers; {mcp_status}); "
          f"webhook pusher on", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
