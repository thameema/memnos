"""memnos MCP server — makes memnos Claude-native (Claude Code / Desktop / API).

Thin adapter: each tool call forwards to the hardened memnos HTTP server with a
Bearer token, so ALL the production guarantees apply unchanged (auth, namespace
ACL, audit, usage ledger, pooling). No memory logic here — one core, thin
adapters (the integration principle).

The SAME tool definitions below back TWO transports:
  - stdio (this file run directly, via run_stdio()): one token/namespace for the
    life of the process, from env vars (below). Self-re-execs in place when
    `memnos upgrade` swaps installed files out from under it — see the
    "self-re-exec on version change" section near the bottom (issue #68).
  - streamable-HTTP (mounted at :8900/mcp by memnos_server.py, via
    mcp.streamable_http_app()): a DIFFERENT caller can hit every request, so
    token/namespace come from _REQUEST_CTX instead — a ContextVar the HTTP
    mount's ASGI wrapper sets per-request from the incoming Authorization /
    X-Memnos-Namespace headers before the tool call runs. See memnos_server.py.

Configure (env, stdio only):
  MEMNOS_URL    default http://127.0.0.1:8900
  MEMNOS_TOKEN  Bearer token from `python memnos_admin.py token <principal>`
  MEMNOS_NS     namespace scope for this agent/user (e.g. user:alice, team:eng)

Wire into Claude Code (~/.claude/settings.json mcpServers), Claude Desktop, etc.:
  { "memnos": { "command": "/path/.venv/bin/python", "args": ["/path/memnos_mcp.py"],
                "env": { "MEMNOS_TOKEN": "mnk_...", "MEMNOS_NS": "user:alice" } } }
"""
import contextvars
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import httpx
from mcp.server.fastmcp import FastMCP
try:
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:                                   # very old SDKs — fall back to RuntimeError
    ToolError = RuntimeError
import offline_queue
try:
    # Eager (not lazy) on purpose — issue #68's immediate-safety half: a runtime
    # `import nsresolve` deep inside a request (the old shape, see _ns_source/remember
    # below) is exactly the kind of file open that can throw FileNotFoundError if
    # `memnos upgrade` swaps package files out from under this running process in the
    # window before the re-exec watcher (further down this file) fires. Importing here,
    # at module load, means that race can only ever affect process START, which already
    # fails loudly and obviously — never a request mid-flight.
    import nsresolve
except Exception:                                   # very old installs without nsresolve.py
    nsresolve = None

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900").rstrip("/")
TOKEN = os.environ.get("MEMNOS_TOKEN", "")
_OVR = os.path.join(os.path.expanduser("~"), ".memnos", "ns_overrides.json")

# Per-request (token, namespace) for the HTTP-mounted transport — None on stdio,
# where the module-level TOKEN/env resolution below applies instead.
_REQUEST_CTX = contextvars.ContextVar("_memnos_mcp_request_ctx", default=None)

# ---- in-flight tool-call tracking (issue #68) -----------------------------------------
# Incremented/decremented around every tool dispatch (both transports — see
# _ReexecAwareFastMCP.call_tool below); only ever READ by the stdio re-exec watcher
# further down, which is why a plain Lock/Condition (not asyncio-flavored) is fine: the
# watcher lives on its own real OS thread, deliberately outside the asyncio event loop
# (see _wait_idle_and_reexec's docstring for why re-exec must never run ON that loop).
#
# _last_activity is updated (under the SAME lock) on every enter AND every exit, not
# just when the count reaches zero — the watcher's idle wait is timed off of it rather
# than off two widely-spaced samples of the counter. A call that both starts and
# finishes well inside the watcher's grace-period sleep (e.g. an immediate ToolError
# for an unknown tool name — no I/O, sub-millisecond) can toggle the counter 0->1->0
# entirely between two polls of a fixed-interval polling loop; timing off
# _last_activity instead means ANY activity — however brief — is observed via the
# Condition's notify() (which always fires before the lock is released) and restarts
# the grace period, so a fleeting blip can never be missed by unlucky sampling.
_inflight_lock = threading.Lock()
_inflight_cond = threading.Condition(_inflight_lock)
_inflight_count = 0
_last_activity = time.monotonic()


def _enter_call():
    global _inflight_count, _last_activity
    with _inflight_cond:
        _inflight_count += 1
        _last_activity = time.monotonic()
        _inflight_cond.notify_all()


def _exit_call():
    global _inflight_count, _last_activity
    with _inflight_cond:
        _inflight_count -= 1
        _last_activity = time.monotonic()
        _inflight_cond.notify_all()


class _ReexecAwareFastMCP(FastMCP):
    """FastMCP subclass — NOT a `mcp.call_tool = ...` monkeypatch — because
    `FastMCP.__init__` -> `_setup_handlers()` captures `self.call_tool` as a bound
    method INTO the lowlevel Server's `request_handlers` dict at construction time;
    a later instance-attribute assignment would be invisible to that already-captured
    reference. Subclassing participates in normal method resolution instead. (In-flight
    tool-call tracking for the re-exec watcher lives one layer further OUT than
    call_tool — see _install_inflight_wrapper, further down — so this override exists
    for run_stdio_async() alone.)

    run_stdio_async(): identical to FastMCP's, except the ServerSession's
    initialization gate is relaxed to the SAME 'stateless' mode already used for the
    HTTP mount (issue #37 Layer 1) — but ONLY for a process that IS a re-exec'd
    resumption (MEMNOS_ADAPTER_REEXEC_RESUMED=1, set on this process's own environment
    right before the execv() that produced it — see _do_reexec), never for a fresh
    launch. This is what makes a re-exec (issue #68) transparent to the client without
    weakening protocol enforcement for the normal case: the MCP host (Claude Code,
    Claude Desktop, ...) sent `initialize` exactly once, at the start of a connection
    it still believes is unbroken, and will never send it again after this process
    image gets swapped. A freshly-constructed, non-stateless ServerSession starts
    NotInitialized and rejects the very next tool call with "Received request before
    initialization was complete" (mcp.server.session.ServerSession) — silently breaking
    the exact session execv is supposed to keep alive. A first, non-resumed launch
    keeps FastMCP's normal strict stdio behavior unchanged."""

    async def run_stdio_async(self) -> None:
        from mcp.server.stdio import stdio_server
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream, write_stream,
                self._mcp_server.create_initialization_options(),
                stateless=_is_resumed_session(),
            )


mcp = _ReexecAwareFastMCP(
    "memnos",
    # HTTP mount only (memnos_server.py) — irrelevant to mcp.run() stdio below.
    # stateless_http: a fresh transport per request, no server-side session to
    # lose — so a memnos restart never forces a client-side session restart
    # (issue #37 Layer 1's acceptance bar). json_response: plain JSON per POST,
    # no SSE streaming needed for request/response tool calls.
    stateless_http=True, json_response=True)


def _config_dir():
    """Resolved fresh on every call (not cached at import time) so tests can point HOME
    at a temp dir without needing a module reload."""
    return os.path.join(os.path.expanduser("~"), ".memnos")


def _drain_offline_queue():
    """Opportunistically replay any queued writes now that we know the server answered
    (issue #37 Layer 3). Called at adapter startup and after every successful write —
    this is the ONLY drain path for hosts that never run the Claude Code hooks (Claude
    Desktop, omnigent, or any other MCP host talking to this same adapter), so a queued
    write here must not depend on `memnos hook status` ever running. Best-effort: a
    failed drain must never break the write that triggered it, so any exception here
    yields (0, 0) rather than propagating.

    Returns (drained, rejected). A permanently-rejected item (401/403/400-class, see
    offline_queue.drain()) is otherwise invisible to the caller — this adapter has no
    separate status/health tool to poll, so callers below fold `rejected` into the
    text they return for the tool call that triggered the drain. Also logged to stderr
    unconditionally (not just returned) so the startup-time call further down — before
    any tool call exists to attach a note to — doesn't drop a rejection silently.

    `TOKEN` here is only ever a FALLBACK (see offline_queue.drain()'s `fallback_token`):
    each item drains with its own captured token when it has one (issue #45). On stdio
    TOKEN is the real per-process token and matches what every item already carries. On
    the HTTP mount TOKEN is permanently empty (the mount has no token of its own) — that
    used to mean every drain 401'd; now it's simply unused whenever an item has its own."""
    try:
        drained, rejected = offline_queue.drain(_config_dir(), URL, TOKEN, timeout=8)
    except Exception:
        return 0, 0
    if rejected:
        print(f"memnos: ⚠ {rejected} previously-queued write{'s' if rejected != 1 else ''} "
              f"permanently rejected during drain — see {_config_dir()}/offline_queue/*.rejected",
              file=sys.stderr)
    return drained, rejected


def _rejected_note(rejected: int) -> str:
    if not rejected:
        return ""
    return (f"\n(⚠ {rejected} previously-queued write{'s' if rejected != 1 else ''} "
            f"permanently rejected — see offline_queue/*.rejected)")


_drain_offline_queue()          # flush anything queued before this adapter process started


def _git_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return r or None
    except Exception:
        return None


def _ns_source():
    """Resolve the namespace the SAME way the hooks/CLI do (nsresolve.resolve_with_source),
    so explicit tool calls and auto-save/recall never target different buckets, AND we know
    whether it's a real binding or a default fallback (issue #20, Part B). Returns
    (namespace, source). Falls back to the legacy local resolution if nsresolve isn't
    importable (very old installs).

    HTTP mount: if _REQUEST_CTX is set (this call is running inside a request handled by
    the streamable-HTTP transport), its namespace ALWAYS wins — cwd/git-root resolution
    is meaningless for a shared server handling requests from many different callers."""
    ctx = _REQUEST_CTX.get()
    if ctx is not None:
        return ctx[1], "http-header"
    if nsresolve is not None:
        try:
            return nsresolve.resolve_with_source()
        except Exception:
            pass
    cwd = os.getcwd()
    root = _git_root()
    try:
        m = json.load(open(_OVR))
        for k in (cwd, os.path.realpath(cwd), root):
            if k and m.get(k):
                return m[k], "legacy"
    except Exception:
        pass
    env = os.environ.get("MEMNOS_NS", "").strip()
    if env and env.lower() != "auto":
        return env, "env"
    return ("proj:" + (os.path.basename(root) if root
                       else (os.path.basename(cwd.rstrip("/")) or "default")), "default")


def _ns():
    return _ns_source()[0]


# expose the resolved namespace on every tool result so it's never ambiguous
NS = _ns()


def _token():
    """Bearer token for the loopback call to the memnos HTTP server. HTTP mount: the
    CALLER's own already-validated token (forwarded as-is, never re-minted) so the REST
    layer's auth/ACL/audit runs exactly as it would for any other caller. stdio: the
    fixed env-configured token for this process."""
    ctx = _REQUEST_CTX.get()
    return ctx[0] if ctx is not None else TOKEN


def _post(path, payload, timeout=60):
    try:
        r = httpx.post(f"{URL}{path}", json={"namespace": _ns(), **payload},
                       headers={"Authorization": f"Bearer {_token()}"}, timeout=timeout)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # fail fast + clearly — a down server must never surface as a cryptic traceback
        raise RuntimeError(f"memnos server is not running at {URL} — "
                           "ask the user to run `memnos start`") from None
    if r.status_code == 503:
        raise RuntimeError("memnos: database unreachable (Postgres may be down) — "
                           "ask the user to check `memnos status`")
    r.raise_for_status()
    return r.json()


def _write_error(e, what):
    """Bug 4: a FAILED write must surface as an unmistakable MCP tool ERROR (isError=true) so
    the model tells the user the save FAILED — never a soft string the model glosses over as
    success. Build the message; the caller raises ToolError with it."""
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        if c == 401:
            return (f"memnos {what} FAILED — NOT saved (401 unauthorized: the MEMNOS_TOKEN is "
                    f"invalid or revoked). Tell the user the memory was NOT stored.")
        if c == 403:
            rbody = {}
            try:
                rbody = e.response.json()
            except Exception:
                pass
            wns = rbody.get("writable_namespaces")
            if wns:
                ns_list = ", ".join(wns)
                return (f"memnos {what} FAILED — NOT saved (403 forbidden: write rejected for "
                        f"namespace '{_ns()}'). Your token can write to: {ns_list}. "
                        f"Switch with: /memnos ns=<namespace>  or  MEMNOS_NS=<namespace>")
            return (f"memnos {what} FAILED — NOT saved (403 forbidden: this principal is not "
                    f"granted write access to namespace '{_ns()}'). Tell the user the memory "
                    f"was NOT stored and that the agent's token needs a grant on this namespace.")
        if c == 400:
            detail = ""
            try:
                detail = f" — {e.response.json().get('error', '')}"
            except Exception:
                pass
            return f"memnos {what} FAILED — NOT saved (400 bad request{detail})."
        return f"memnos {what} FAILED — NOT saved (HTTP {c}). The memory was NOT stored."
    return f"memnos {what} FAILED — NOT saved ({type(e).__name__}: {e}). The memory was NOT stored."


@mcp.tool()
def recall(query: str) -> str:
    """Search the user's long-term memory for information relevant to the query.
    Returns remembered facts and statements (current values preferred over superseded
    ones). Call this whenever the user references past conversations, stated
    preferences, prior decisions, people/projects, or context not in this session.

    On a TRANSIENT outage (server unreachable) this serves the last-synced snapshot
    from THIS SAME namespace instead of nothing — the response is explicitly prefixed
    "(STALE — ...)" so it is never mistaken for a live answer (issue #37 Layer 3)."""
    ns = _ns()
    try:
        out = _post("/recall", {"query": query})
        ctx = out.get("context", "")
        offline_queue.save_snapshot(_config_dir(), ns, ctx, len(out.get("memories") or []))
        _, rejected = _drain_offline_queue()
        note = _rejected_note(rejected)
        # tell the chat client which namespace we searched so it's never ambiguous.
        header = f"(recalled from '{ns}')\n"
        return (header + ctx + note) if ctx else f"(no relevant memories found in '{ns}'){note}"
    except Exception as e:
        if offline_queue.is_transient(e):
            snap = offline_queue.load_snapshot(_config_dir(), ns)
            if snap and snap.get("context"):
                age = offline_queue.format_snapshot_age(snap)
                return (f"(STALE — memnos is unreachable; showing the last-synced snapshot "
                        f"from {age} for '{ns}'. This may be outdated.)\n" + snap["context"])
            return f"(memnos recall unavailable: {e})"
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code == 401:
                return "(memnos: unauthorized — check MEMNOS_TOKEN)"
            if code == 403:
                # _ns(), not the module-level NS global: on the HTTP-mounted transport a
                # different caller/namespace can hit every request (see _ns_source), so the
                # stale import-time NS would report the wrong namespace here.
                return f"(memnos: not authorized for namespace {_ns()})"
            return f"(memnos recall error: HTTP {code})"
        return f"(memnos recall unavailable: {e})"


@mcp.tool()
def remember(text: str, memory_type: str = "") -> str:
    """Save a durable fact, preference, decision, or piece of context to the user's
    long-term memory for future sessions. Use for things worth keeping (preferences,
    project facts, commitments, identity) — not transient chatter. If this updates a
    prior fact (e.g. a changed preference), memnos supersedes the old value automatically.

    Pass memory_type="constraint" when the text should GOVERN future behavior (a rule
    to always/never follow) rather than merely describe the world — constraint memories
    are PINNED into every future recall for this namespace instead of competing for
    relevance like an ordinary fact. Other allowed types: decision, incident, skill,
    fact. Leave empty for an ordinary untyped memory."""
    ns, source = _ns_source()
    body = {"text": text, "speaker": "user", "async": True}
    if memory_type:
        body["type"] = memory_type
    try:
        # async:true — server stores the raw turn immediately and extracts facts in the
        # background, so a slow local-LLM extraction backend (Ollama 30-80s) can't ReadTimeout
        # and drop the write. The raw turn is durable the moment this returns.
        out = _post("/remember", body)
    except Exception as e:
        # issue #37 Layer 3: a TRANSIENT failure (server down, or a 5xx from an
        # embed-time/adapter-time error) must never surface as "FAILED — NOT saved" —
        # that's exactly the failure mode that pushes a session toward improvising some
        # OTHER store. Enqueue into memnos's own offline_queue (replays into this SAME
        # store once healthy) and tell the caller it's saved. A PERMANENT failure
        # (401/403/400 — Bug 4's contract) still raises unchanged below.
        if offline_queue.is_transient(e):
            # issue #45: capture THIS request's own token (the caller's, under the HTTP
            # mount — see _token()) alongside the item, so a later drain replays it as
            # THIS caller/tenant rather than whatever the drainer's own module-level
            # TOKEN happens to be (empty under the HTTP mount, since the mount has no
            # token of its own).
            offline_queue.enqueue(_config_dir(), ns, text, "user", memory_type=memory_type,
                                   token=_token())
            return (f"remembered in '{ns}' (queued — memnos is temporarily unreachable; "
                    f"will sync automatically once it recovers, nothing lost)")
        raise ToolError(_write_error(e, "remember")) from None
    _, rejected = _drain_offline_queue()
    # write-time attribution (issue #20, Part B): always name the destination namespace so
    # the chat client relays WHERE the memory landed.
    dest = out.get("namespace") or ns
    if out.get("extraction") == "queued":
        msg = f"remembered in '{dest}' (turn {out.get('turn_id')}; facts extracting in background)"
    else:
        msg = f"remembered in '{dest}' (turn {out.get('turn_id')}, {out.get('facts', 0)} facts extracted)"
    # default-fallback: no binding for this repo — surface the one-step bind offer.
    if source == "default" and nsresolve is not None:
        try:
            msg += "\n" + nsresolve.default_fallback_hint(dest)
        except Exception:
            pass
    sugg = out.get("suggestion")              # advisory only — the write already landed in `dest`
    if isinstance(sugg, dict) and sugg.get("namespace"):
        msg += (f"\nhint: this looks like '{sugg['namespace']}' ({sugg.get('reason','')}) — "
                f"bind future writes there if so.")
    msg += _rejected_note(rejected)
    return msg


@mcp.tool()
def consolidate() -> str:
    """Run memory consolidation (the offline 'sleep' pass): distill recent episodes
    into durable semantic facts and entity dossiers, resolving contradictions. Normally
    run on a schedule; expose here for on-demand use."""
    try:
        out = _post('/consolidate', {})
    except Exception as e:
        raise ToolError(_write_error(e, "consolidate")) from None
    return f"consolidated: {out}"


def _err(e, what):
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        if c == 401: return "(memnos: unauthorized — check MEMNOS_TOKEN)"
        if c == 403: return f"(memnos: not authorized for namespace {_ns()})"
        if c == 404: return f"(memnos: not found)"
        return f"(memnos {what} error: HTTP {c})"
    return f"(memnos {what} unavailable: {e})"


@mcp.tool()
def recall_wide(query: str) -> str:
    """Recall ACROSS ALL namespaces your key is permitted to read (not just the current
    one). Use when the answer might live in a related namespace — e.g. your default is
    one project but the fact is in another you can access. Results are tagged with the
    namespace they came from."""
    try:
        out = _post("/recall", {"query": query, "scope": "all"})
        ctx = out.get("context", "")
        ns = out.get("namespaces_searched", [])
        return (f"[searched {len(ns)} namespaces: {', '.join(ns)}]\n" + ctx) if ctx else "(no relevant memories found)"
    except Exception as e:
        return _err(e, "recall_wide")


@mcp.tool()
def memory_search(query: str) -> str:
    """Search long-term memory and return the raw ranked memories (facts + raw turns)
    as JSON. Like recall(), but returns structured rows rather than a formatted context
    block — useful when you want to inspect or post-process individual memories."""
    try:
        rows = _post("/memory/search", {"query": query}).get("memories", [])
        return str(rows) if rows else "(no matching memories)"
    except Exception as e:
        return _err(e, "memory_search")


@mcp.tool()
def memory_write(text: str) -> str:
    """Write a durable memory (alias of remember). Stores the text and extracts
    bi-temporal facts; supersedes a changed single-valued fact automatically."""
    ns = _ns()
    try:
        out = _post("/memory/write", {"text": text, "speaker": "user"})
    except Exception as e:
        # issue #37 Layer 3 — same transient/permanent split as remember() above.
        # /memory/write is a strict server-side alias of /remember (same handler), so
        # the queued item replays through the shared drain's POST /remember unchanged.
        if offline_queue.is_transient(e):
            offline_queue.enqueue(_config_dir(), ns, text, "user", token=_token())
            return f"written to '{ns}' (queued — memnos is temporarily unreachable; will sync automatically once it recovers, nothing lost)"
        raise ToolError(_write_error(e, "memory_write")) from None
    _, rejected = _drain_offline_queue()
    return f"written (turn {out.get('turn_id')}, {out.get('facts', 0)} facts)" + _rejected_note(rejected)


@mcp.tool()
def memory_delete(id: int) -> str:
    """Delete (system-time expire) a semantic fact by its numeric id. The fact is excluded
    from all future retrieval; history is preserved (never hard-deleted). Get ids from
    memory_search or get_entity."""
    try:
        out = _post("/memory/delete", {"id": int(id)})
    except Exception as e:
        raise ToolError(_write_error(e, "memory_delete")) from None
    return f"deleted fact {out.get('deleted')}: {out.get('statement','')}"


@mcp.tool()
def get_entity(name: str, depth: int = 1) -> str:
    """Look up an entity (person/project/place) and return its graph neighbourhood
    (related entities + edge weights) and the facts that mention it. depth 1-3 expands
    further over the relationship graph."""
    try:
        return str(_post("/entity", {"name": name, "depth": depth}))
    except Exception as e:
        return _err(e, "get_entity")


@mcp.tool()
def get_related(name: str) -> str:
    """Return the directly-related entities for a given entity (the adjacency list),
    ranked by co-mention strength."""
    try:
        return str(_post("/related", {"name": name}).get("related", []))
    except Exception as e:
        return _err(e, "get_related")


@mcp.tool()
def graph_query(entities: list[str], hops: int = 2) -> str:
    """Relationship reasoning: seed from one or more entities, expand N hops over the
    knowledge graph, and return the facts mentioned by the reachable entities. Read-only."""
    try:
        return str(_post("/graph", {"entities": entities, "hops": hops}).get("facts", []))
    except Exception as e:
        return _err(e, "graph_query")


@mcp.tool()
def community_search(name: str) -> str:
    """Find the community (densely-connected cluster of entities) that a given entity
    belongs to — the people/projects/things it's most associated with."""
    try:
        return str(_post("/community", {"name": name}))
    except Exception as e:
        return _err(e, "community_search")


@mcp.tool()
def check_contradictions() -> str:
    """List potential contradictions in this namespace: currently-valid facts where the
    same subject+predicate has more than one distinct value (e.g. lives in two places).
    A non-blocking review signal."""
    try:
        c = _post("/contradictions", {}).get("contradictions", [])
        return str(c) if c else "(no contradictions detected)"
    except Exception as e:
        return _err(e, "check_contradictions")


@mcp.tool()
def reconcile_claim(statement: str, subject: str = "", predicate: str = "") -> str:
    """Check a claim you hold elsewhere (e.g. a local note / CLAUDE.md you treat as truth)
    against memnos. If memnos holds a CURRENT, different value about the same subject, this
    flags it as a likely STALE local memory — return the memnos value + its date so you can
    tell the user their local memory is out of date. Pass the subject/predicate you parsed
    from the claim (e.g. subject='TAP', predicate='uses'). Returns {stale, conflicts, matches}."""
    try:
        body = {"statement": statement}
        if subject:
            body["subject"] = subject
        if predicate:
            body["predicate"] = predicate
        out = _post("/reconcile", body)
        if out.get("stale"):
            return ("STALE/contradiction — memnos holds a different current value: "
                    + str(out.get("conflicts")))
        return "no conflict — memnos agrees or has nothing newer: " + str(out.get("matches") or "(no related facts)")
    except Exception as e:
        return _err(e, "reconcile_claim")


@mcp.tool()
def knowledge_health() -> str:
    """Return a knowledge-health report for this namespace: a 0-100 score plus signals
    (current/superseded/expired facts, entities, orphan entities, contradiction groups)."""
    try:
        return str(_post("/knowledge/health", {}))
    except Exception as e:
        return _err(e, "knowledge_health")


@mcp.tool()
def namespace_subscribe(webhook: str = "") -> str:
    """Subscribe to this namespace's memory stream. Returns a subscription_id and a cursor;
    new memories written after this call are delivered via namespace_feed (poll). Pass an
    optional webhook URL to record it for push delivery."""
    try:
        return str(_post("/subscribe", {"webhook": webhook} if webhook else {}))
    except Exception as e:
        raise ToolError(_write_error(e, "namespace_subscribe")) from None


@mcp.tool()
def namespace_feed(subscription_id: int) -> str:
    """Poll new memories for a subscription since its last cursor (advances the cursor).
    Returns the new memory items and the updated cursor."""
    try:
        return str(_post("/feed", {"subscription_id": int(subscription_id)}))
    except Exception as e:
        return _err(e, "namespace_feed")


@mcp.tool()
def corpus_ingest(name: str, text: str, kind: str = "doc") -> str:
    """Ingest an architecture document (LLD/HLD/ADR): extract its normative constraints
    (SHALL/MUST/REQUIRED/...) and store them as searchable constraint memories under `name`."""
    try:
        out = _post("/corpus/ingest", {"name": name, "text": text, "kind": kind})
    except Exception as e:
        raise ToolError(_write_error(e, "corpus_ingest")) from None
    return f"ingested {out.get('constraints', 0)} constraints from '{name}'"


@mcp.tool()
def corpus_check(snippet: str) -> str:
    """Return the architecture constraints relevant to a code snippet (does this code
    violate a documented SHALL/MUST rule?). Read-only; ranked by relevance."""
    try:
        c = _post("/corpus/check", {"snippet": snippet}).get("constraints", [])
        return str(c) if c else "(no relevant constraints found)"
    except Exception as e:
        return _err(e, "corpus_check")


@mcp.tool()
def corpus_check_diff(diff: str, name: str = "") -> str:
    """Check a unified diff (git diff / GitHub PR patch text) against the architecture
    corpus. Returns a verdict per relevant constraint — violated, satisfied, or
    uncovered (topically relevant, no clear evidence either way) — plus an overall
    compliance score. Optional `name` restricts the check to one ingested source.
    Read-only, deterministic, no LLM."""
    try:
        body = {"diff": diff}
        if name:
            body["name"] = name
        return str(_post("/corpus/check_diff", body))
    except Exception as e:
        return _err(e, "corpus_check_diff")


@mcp.tool()
def corpus_list() -> str:
    """List the architecture documents ingested into this namespace and their constraint counts."""
    try:
        return str(_post("/corpus/list", {}).get("sources", []))
    except Exception as e:
        return _err(e, "corpus_list")




@mcp.tool()
def get_entity_dossier(entity: str) -> str:
    """Return the stored dossier (summary paragraph) for an entity by name. The dossier
    is generated during consolidation when MEMNOS_ENTITY_DOSSIERS=1 is set. Returns the
    summary text, or a message explaining that none has been generated yet."""
    try:
        out = _post("/entity/dossier", {"entity": entity})
        text = out.get("dossier", "")
        if not text:
            return f"(no dossier found for '{entity}')"
        gen = out.get("generated_at") or ""
        model = out.get("model_used") or ""
        header = f"Dossier for '{out.get('entity', entity)}'"
        if gen:
            header += f" (generated {gen[:10]}"
            if model:
                header += f" by {model}"
            header += ")"
        return header + ":\n" + text
    except Exception as e:
        if hasattr(e, "response") and e.response.status_code == 404:
            return f"(no dossier found for '{entity}' yet -- run consolidate with MEMNOS_ENTITY_DOSSIERS=1 to generate)"
        return _err(e, "get_entity_dossier")

@mcp.tool()
def ingest_file(filename: str, text: str, extract: bool = False) -> str:
    """Ingest a document's text into memory: it's chunked and each chunk stored as a
    searchable memory under `filename`. Pass already-extracted text (md/txt/code, or
    text you pulled from a PDF/DOCX). Set extract=true to also pull bi-temporal facts."""
    try:
        out = _post("/ingest/file", {"filename": filename, "text": text, "extract": extract})
    except Exception as e:
        raise ToolError(_write_error(e, "ingest_file")) from None
    return f"ingested '{out.get('filename')}' as {out.get('chunks', 0)} chunks"


@mcp.tool()
def segment_episodes(gap_minutes: int = 30) -> str:
    """Build the episodic memory tier: group recent raw turns into coherent episodes
    (boundary on session change or a time gap), each with a summary + time span. Run after
    a batch of activity. Incremental + idempotent."""
    try:
        return f"segmented {_post('/episode/segment', {'gap_minutes': gap_minutes}).get('episodes', 0)} episodes"
    except Exception as e:
        return _err(e, "segment_episodes")


@mcp.tool()
def recall_episodes(query: str) -> str:
    """Recall whole EPISODES relevant to a query (event-level memory: 'the session where X
    happened') rather than scattered facts. Hybrid search over episode summaries."""
    try:
        eps = _post("/episode/recall", {"query": query}).get("episodes", [])
        return str(eps) if eps else "(no matching episodes)"
    except Exception as e:
        return _err(e, "recall_episodes")


@mcp.tool()
def get_episode(id: int) -> str:
    """Fetch one episode in full: its verbatim turns and the facts derived from it."""
    try:
        return str(_post("/episode", {"id": int(id)}))
    except Exception as e:
        return _err(e, "get_episode")


@mcp.tool()
def decay_episodes(half_life_days: int = 30) -> str:
    """Run episodic decay: re-score episode salience by recency (half-life) + access
    frequency so old, unused episodes fade while recent/recalled ones stay sharp. Semantic
    facts are not affected."""
    try:
        return f"decayed {_post('/episode/decay', {'half_life_days': half_life_days}).get('updated', 0)} episodes"
    except Exception as e:
        return _err(e, "decay_episodes")


@mcp.tool()
def copy_memories_from(src: str, mode: str = "copy", like: str = "") -> str:
    """Copy (or move) memories from another namespace INTO the current one. `src` is the
    source namespace; mode 'copy' duplicates (optional `like` substring filter on the text),
    mode 'move' relocates the whole source namespace. You must have read on the source and
    write on the current namespace. The entity graph is rebuilt in the destination."""
    try:
        body = {"src": src, "mode": mode}
        if like:
            body["like"] = like
        out = _post("/namespace/copy", body)
    except Exception as e:
        raise ToolError(_write_error(e, "copy_memories_from")) from None
    return (f"{out.get('mode')}d {out.get('facts',0)} facts + {out.get('raw_turns',0)} turns "
            f"from {src} into {_ns()}")


@mcp.tool()
def get_provenance(id: int) -> str:
    """Show the evidence chain for a remembered fact: the verbatim source turn(s) it was
    extracted from (or, for a dossier, the turns its source facts derived from). Answers
    'why do you believe this?'. Get ids from memory_search or get_entity."""
    try:
        return str(_post("/provenance", {"id": int(id)}))
    except Exception as e:
        return _err(e, "get_provenance")


@mcp.tool()
def get_context(query: str) -> str:
    """Return a ready-to-paste context block for a query (same as recall) — no LLM at
    query time."""
    try:
        return _post("/memory/context", {"query": query}).get("context", "") or "(no relevant memories)"
    except Exception as e:
        return _err(e, "get_context")


@mcp.tool()
def lease_acquire(key: str, holder_id: str, ttl_seconds: int = 1200) -> str:
    """Atomically acquire an exclusive lease on a work item (e.g. 'ticket:PROJ-543',
    'mr:!51', 'repo:example-platform'). Returns granted=true if you now hold the lease;
    granted=false with held_by/expires_at if another agent is already working it.
    Always call this before starting work on any shared item; heartbeat while working;
    release on completion."""
    try:
        out = _post("/lease/acquire", {"key": key, "holder_id": holder_id, "ttl_seconds": ttl_seconds})
        if out.get("granted"):
            return f"granted lease on '{key}' until {out['expires_at']}"
        hb = out.get("holder_id") or "unknown"
        exp = out.get("expires_at") or "unknown"
        return f"denied — '{key}' held by {hb} until {exp}"
    except Exception as e:
        return _err(e, "lease_acquire")


@mcp.tool()
def lease_heartbeat(key: str, holder_id: str, ttl_seconds: int = 1200) -> str:
    """Extend the expiry of a lease you hold. Call every ttl/3 seconds while working
    to prevent the lease from expiring and being stolen."""
    try:
        out = _post("/lease/heartbeat", {"key": key, "holder_id": holder_id, "ttl_seconds": ttl_seconds})
        if out.get("renewed"):
            return f"heartbeat accepted — lease on '{key}' extended to {out['expires_at']}"
        return f"heartbeat rejected — lease on '{key}' not found or expired"
    except Exception as e:
        return _err(e, "lease_heartbeat")


@mcp.tool()
def lease_release(key: str, holder_id: str) -> str:
    """Release a lease you hold on a work item. Call this when work is complete or
    abandoned. A crashed holder's lease auto-expires after the TTL."""
    try:
        out = _post("/lease/release", {"key": key, "holder_id": holder_id})
        return f"released lease on '{key}'" if out.get("released") else f"lease on '{key}' not found or not yours"
    except Exception as e:
        return _err(e, "lease_release")


@mcp.tool()
def lease_who_holds(key: str) -> str:
    """Check who currently holds the lease on a work item. Returns holder_id and
    expiry if held, or confirms the item is free."""
    try:
        out = _post("/lease/who_holds", {"key": key})
        if out.get("held"):
            return f"'{key}' held by {out['holder_id']} until {out['expires_at']}"
        return f"'{key}' is free"
    except Exception as e:
        return _err(e, "lease_who_holds")


@mcp.tool()
def lease_list() -> str:
    """List all active leases in the current namespace — useful to see what work
    items other agents are currently processing."""
    try:
        leases = _post("/lease/list", {}).get("leases", [])
        if not leases:
            return "no active leases in namespace"
        lines = [f"  {l['key']} — held by {l['holder_id']} until {l['expires_at']}" for l in leases]
        return f"{len(leases)} active lease(s):\n" + "\n".join(lines)
    except Exception as e:
        return _err(e, "lease_list")


# ---- self-re-exec on version change (issue #68) ---------------------------------------
#
# Problem: `memnos mcp` is a long-lived stdio process. `memnos upgrade` (uv tool
# install / pip install -U) swaps memnos's installed files out from under it while it
# keeps running the OLD in-memory code — every load-bearing import already happened at
# module-load time, and CPython never re-reads an already-imported source file off
# disk — with no way to pick up the new version short of the user manually restarting
# their entire coding session.
#
# Fix: notice a new build on disk (background watcher thread) and swap this process's
# own image in place once no tool call is in flight. os.execv() replaces the process
# image but keeps the SAME PID and the SAME stdin/stdout file descriptors already
# connected to the MCP host — they are not CLOEXEC (PEP 446 only makes NEWLY-opened
# files close-on-exec by default; the standard streams stay inheritable) — so the host
# never sees an exit/restart: no dropped connection, no FileNotFoundError, no manual
# session restart. Escape hatch: MEMNOS_ADAPTER_REEXEC=0 disables the watcher entirely
# (today's manual-restart behavior).
#
# Scope: only run_stdio() (below — the real entry point for `memnos mcp` / `python
# memnos_mcp.py`, see memnos_cli.cmd_mcp) starts the watcher. The HTTP-mounted
# transport (memnos_server.py's streamable_http_app(), used for the REST API /
# omnigent-direct) never calls run_stdio() and is unaffected — that process serves many
# tenants at once from one long-running server with its own restart story, well outside
# the "single long-lived desktop session" problem this issue is about.

def _reexec_enabled() -> bool:
    return os.environ.get("MEMNOS_ADAPTER_REEXEC", "1").strip().lower() not in (
        "0", "false", "no", "off", "")


_RESUMED_ENV = "MEMNOS_ADAPTER_REEXEC_RESUMED"


def _is_resumed_session() -> bool:
    """True iff THIS process is the result of a re-exec (issue #68) — see
    _ReexecAwareFastMCP.run_stdio_async, which uses this to relax stdio's
    initialization gate only for a resumed session, never for a fresh launch. Set on
    this process's own environment right before the execv() call that replaced it (see
    _do_reexec) — os.execv() doesn't take an explicit environment, it inherits the
    calling process's current one, and os.environ mutations write straight through to
    that (os.putenv underneath), so this is visible to the re-exec'd program from its
    very first line. Sticky across any FURTHER re-exec too, since it stays set in the
    inherited environment — correct: once resumed, a session is a resumed session for
    the rest of its life, however many more upgrades it lives through."""
    return os.environ.get(_RESUMED_ENV, "") == "1"


def _reexec_interval_s() -> float:
    try:
        return max(1.0, float(os.environ.get("MEMNOS_ADAPTER_REEXEC_INTERVAL_S", "20")))
    except (TypeError, ValueError):
        return 20.0


def _reexec_drain_grace_s() -> float:
    """Extra delay the in-flight counter (see _enter_call/_exit_call above) must stay
    at zero, continuously, before actually re-exec'ing (see _wait_idle_and_reexec).
    The outermost layer that decrements it (_install_inflight_wrapper's wrapper around
    the lowlevel CallToolRequest handler) still returns BEFORE the lowlevel MCP
    dispatcher has serialized that result and written it to stdout (which
    mcp.server.stdio flushes on every message) — that write happens in code the
    counter doesn't span. This leaves a real, if brief, window between 'the call
    finished running' and 'the response actually reached the host' that the counter
    alone doesn't cover — this grace period closes it. Configurable (default kept
    small) so tests don't need to wait a full production cycle to prove the drain
    logic."""
    try:
        return max(0.0, float(os.environ.get("MEMNOS_ADAPTER_REEXEC_DRAIN_GRACE_S", "0.3")))
    except (TypeError, ValueError):
        return 0.3


def _version_signature() -> str:
    """Cheap signature of 'the installed memnos package on disk right now'. Combines
    the installed distribution version (bumps on a real `memnos upgrade` via uv/pip —
    same call memnos_cli._installed_version() already uses) with this module's own file
    mtime+size (also catches a same-version reinstall or an editable/source-checkout
    edit, where the version string alone wouldn't move — e.g. this ticket's own local
    dev loop). Either changing means a new build is on disk. Deliberately no import of
    memnos_cli / core / anything heavy: this runs on a background thread every
    MEMNOS_ADAPTER_REEXEC_INTERVAL_S seconds and must stay cheap enough that polling it
    is never visible on the request path."""
    dist_version = "?"
    try:
        dist_version = importlib.metadata.version("memnos")
    except Exception:
        pass
    file_sig = "?"
    try:
        st = os.stat(os.path.abspath(__file__))
        file_sig = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        pass
    return f"{dist_version}|{file_sig}"


def _resolve_reexec_argv0(argv0: str):
    """Resolve sys.argv[0] to an absolute, existing path to re-launch. A human typing
    `memnos mcp` at a shell can leave argv[0] as the bare command name ("memnos"), not
    an absolute path — unlike the MCP-host-launched case, where agent-setup/hermes
    always write the shutil.which()-resolved absolute path into the host's own config,
    specifically because "GUI apps launch MCP servers with a minimal PATH" (see
    memnos_cli._mcp_launcher()). Returns None if it can't be resolved to a real file, so
    the caller can decline to re-exec into a broken command instead of crashing."""
    if os.path.isabs(argv0) and os.path.exists(argv0):
        return argv0
    resolved = shutil.which(argv0)
    if resolved:
        return os.path.abspath(resolved)
    candidate = os.path.abspath(argv0)
    return candidate if os.path.exists(candidate) else None


def _do_reexec():
    """Replace this process image in place: same PID, same stdio fds already connected
    to the MCP host, so the host never sees an exit/restart. Called only from the
    watcher thread (never from inside a tool call / the asyncio loop — see
    _wait_idle_and_reexec), once the in-flight counter has been at zero for a full
    grace period.

    Deliberately NOT `os.execv(sys.argv[0], sys.argv)` — the form suggested in the
    original ticket. Verified empirically that it raises PermissionError for the
    `python memnos_cli.py mcp` fallback launch path: memnos_cli.py has no shebang line
    and is not marked executable (only the INSTALLED `memnos` console-script — which
    does have a shebang + the exec bit — works with that form). Re-invoking
    sys.executable directly against the resolved, absolute script path works for both
    launch shapes instead — the console-script file is also perfectly valid to run as
    `python <path>` — and doesn't depend on exec bits or shebangs at all. Env vars
    (MEMNOS_URL/TOKEN/NS) are inherited automatically; execv never touches the
    environment."""
    argv0 = _resolve_reexec_argv0(sys.argv[0])
    if argv0 is None:
        print(f"memnos: adapter update detected but could not resolve '{sys.argv[0]}' "
              f"to re-exec — leaving the old code running; restart this session "
              f"manually to pick up the update", file=sys.stderr)
        return
    try:
        _drain_offline_queue()          # belt-and-suspenders — see module docstring
    except Exception:
        pass
    new_argv = [sys.executable, argv0] + sys.argv[1:]
    # Marks the NEXT process image as a resumed session (see _is_resumed_session) —
    # set before execv, which inherits whatever this process's environment holds at
    # the moment of the call (there's no explicit env argument to pass).
    os.environ[_RESUMED_ENV] = "1"
    print(f"memnos: re-exec'ing adapter (pid {os.getpid()}) to pick up the updated "
          f"build — same MCP connection, no session restart needed", file=sys.stderr)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(sys.executable, new_argv)
    except Exception as e:
        # os.execv only ever returns on failure — a successful call never reaches here.
        print(f"memnos: re-exec failed ({type(e).__name__}: {e}) — leaving the old "
              f"code running", file=sys.stderr)


def _wait_idle_and_reexec():
    """Block — on the watcher thread, never the asyncio loop, never inside a tool call
    — until no MCP tool call is in flight AND nothing has entered/exited a call for a
    full, uninterrupted grace period (see _reexec_drain_grace_s), then re-exec.

    Timed off _last_activity (updated — and notify()'d — on every enter AND exit, see
    _enter_call/_exit_call above) rather than off two widely-spaced samples of the
    counter, for two reasons:
      1. The gap between a call finishing (which is when the counter decrements) and
         that call's response actually being serialized and flushed to stdout by the
         lowlevel MCP dispatcher, downstream of it — the counter alone only proves the
         WORK finished, not that the client has seen the result yet.
      2. A call that both starts AND finishes well inside a single fixed-length sleep
         (e.g. an unknown-tool ToolError — no I/O, sub-millisecond) can toggle the
         counter 0->1->0 entirely between two samples of a plain polling loop, which
         would silently miss it. Using Condition.wait(timeout=...) instead means any
         enter/exit — however brief — wakes this loop immediately via notify() (which
         always fires before the mutating call releases the lock), so the grace timer
         restarts off REAL activity instead of off luck.

    Deliberately releases _inflight_lock (the `with` block ends) before calling
    _do_reexec() — NOT held through the swap. Holding it there was tried and rejected:
    it doesn't protect anything (a request that reaches _install_inflight_wrapper's
    _enter_call() would just block waiting for the SAME lock the watcher holds, so
    execv() would wipe it out having *never* incremented the counter — silently worse,
    not better, than the current design, where at least the counter reflects every
    request that has actually been handed to a task by the time we make our final
    check)."""
    grace = _reexec_drain_grace_s()
    with _inflight_cond:
        while True:
            if _inflight_count != 0:
                _inflight_cond.wait()
                continue
            remaining = grace - (time.monotonic() - _last_activity)
            if remaining <= 0:
                break
            _inflight_cond.wait(timeout=remaining)
    _do_reexec()


def _install_inflight_wrapper():
    """Wrap the LOWLEVEL CallToolRequest handler directly (mcp._mcp_server —
    FastMCP.__init__ -> _setup_handlers() already registered one there for
    `self.call_tool`, see _ReexecAwareFastMCP's docstring) — one layer further out
    than _ReexecAwareFastMCP.call_tool. Between a request being parsed off the wire
    and _ReexecAwareFastMCP.call_tool actually starting, the lowlevel dispatcher does
    its own tool lookup, input-schema validation (or the "not listed" log line when
    validation is skipped), all before it ever calls into our code — time our counter
    doesn't cover if it only wraps call_tool(). Counting from here instead — the
    earliest point any of our own code runs for a given request — leaves only genuine
    OS/asyncio scheduling latency (the request has been read + parsed, a task spawned
    for it, but that task hasn't been given a turn to run yet) as the residual gap, not
    several extra lines of SDK-internal logging/validation work on top of it.

    Only installed for the stdio watcher (called from _start_reexec_watcher, never
    unconditionally at import time) — the HTTP-mounted transport's dispatch chain is
    left byte-for-byte untouched when re-exec isn't in play."""
    import mcp.types as _mcp_types
    handlers = mcp._mcp_server.request_handlers
    orig = handlers[_mcp_types.CallToolRequest]

    async def _tracked(req):
        _enter_call()
        try:
            return await orig(req)
        finally:
            _exit_call()

    handlers[_mcp_types.CallToolRequest] = _tracked


_reexec_watcher_started = False


def _start_reexec_watcher():
    """Idempotent — safe to call more than once (e.g. a test re-invoking run_stdio())."""
    global _reexec_watcher_started
    if _reexec_watcher_started:
        return
    _reexec_watcher_started = True
    if not _reexec_enabled():
        print("memnos: MEMNOS_ADAPTER_REEXEC=0 — self-re-exec on upgrade disabled for "
              "this adapter process", file=sys.stderr)
        return
    _install_inflight_wrapper()
    t = threading.Thread(target=_reexec_watch_loop, name="memnos-adapter-reexec", daemon=True)
    t.start()


def _reexec_watch_loop():
    """Poll _version_signature() every MEMNOS_ADAPTER_REEXEC_INTERVAL_S seconds;
    once it differs from what this process started with AND has been read back
    UNCHANGED on two consecutive ticks, hand off to _wait_idle_and_reexec().

    The "unchanged twice in a row" requirement (not "changed once") is a debounce
    against a torn upgrade: `pip install -U` / `uv tool install` rewrites several
    files over some (usually sub-second, but not guaranteed) span of real time, not
    atomically. Acting on the very first observed change risks re-exec'ing into a
    half-written tree — memnos_mcp.py's own new mtime landing while memnos_cli.py /
    core/ are still mid-write, or simply missing — which would crash the freshly
    exec'd process on import and produce exactly the kind of visible exit/restart this
    issue exists to eliminate, just relocated to a new moment. Requiring the signature
    to hold steady across a full extra interval before acting costs one more
    MEMNOS_ADAPTER_REEXEC_INTERVAL_S of latency in the worst case, in exchange for
    never swapping into a state that was still being written when first observed."""
    interval = _reexec_interval_s()
    start_sig = _version_signature()
    prev_sig = start_sig
    while True:
        time.sleep(interval)
        try:
            cur_sig = _version_signature()
        except Exception:
            continue
        if cur_sig == start_sig or cur_sig != prev_sig:
            # unchanged since this process started, or changed again since the LAST
            # check (still being written) — keep watching, don't act yet.
            prev_sig = cur_sig
            continue
        print(f"memnos: detected an updated memnos build on disk (stable across two "
              f"checks) — will re-exec this adapter once idle (checked every "
              f"{interval:.0f}s)", file=sys.stderr)
        _wait_idle_and_reexec()
        return


def run_stdio():
    """Entry point for the stdio transport (`memnos mcp` / `python memnos_mcp.py`).
    Starts the self-re-exec watcher (issue #68) before handing off to mcp.run() —
    scoped to stdio only; the HTTP-mounted transport (memnos_server.py) never calls
    this."""
    _start_reexec_watcher()
    mcp.run()


if __name__ == "__main__":
    run_stdio()        # stdio transport (JSON-RPC over stdin/stdout)
