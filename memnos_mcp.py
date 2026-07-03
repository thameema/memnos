"""memnos MCP server — makes memnos Claude-native (Claude Code / Desktop / API).

Thin stdio adapter: each tool call forwards to the hardened memnos HTTP server with
the configured Bearer token, so ALL the production guarantees apply unchanged
(auth, namespace ACL, audit, usage ledger, pooling). No memory logic here — one core,
thin adapters (the integration principle).

Configure (env):
  MEMNOS_URL    default http://127.0.0.1:8900
  MEMNOS_TOKEN  Bearer token from `python memnos_admin.py token <principal>`
  MEMNOS_NS     namespace scope for this agent/user (e.g. user:alice, team:eng)

Wire into Claude Code (~/.claude/settings.json mcpServers), Claude Desktop, etc.:
  { "memnos": { "command": "/path/.venv/bin/python", "args": ["/path/memnos_mcp.py"],
                "env": { "MEMNOS_TOKEN": "mnk_...", "MEMNOS_NS": "user:alice" } } }
"""
import json
import os
import subprocess
import httpx
from mcp.server.fastmcp import FastMCP
try:
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:                                   # very old SDKs — fall back to RuntimeError
    ToolError = RuntimeError

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900").rstrip("/")
TOKEN = os.environ.get("MEMNOS_TOKEN", "")
_OVR = os.path.join(os.path.expanduser("~"), ".memnos", "ns_overrides.json")

mcp = FastMCP("memnos")


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
    importable (very old installs)."""
    try:
        import nsresolve
        return nsresolve.resolve_with_source()
    except Exception:
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


def _post(path, payload, timeout=60):
    try:
        r = httpx.post(f"{URL}{path}", json={"namespace": _ns(), **payload},
                       headers={"Authorization": f"Bearer {TOKEN}"}, timeout=timeout)
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
                return (f"Write rejected for namespace '{_ns()}'. "
                        f"Your token can write to: {ns_list}\n"
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
    preferences, prior decisions, people/projects, or context not in this session."""
    try:
        ns = _ns()
        ctx = _post("/recall", {"query": query}).get("context", "")
        # tell the chat client which namespace we searched so it's never ambiguous.
        header = f"(recalled from '{ns}')\n"
        return (header + ctx) if ctx else f"(no relevant memories found in '{ns}')"
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 401:
            return "(memnos: unauthorized — check MEMNOS_TOKEN)"
        if code == 403:
            return f"(memnos: not authorized for namespace {NS})"
        return f"(memnos recall error: HTTP {code})"
    except Exception as e:
        return f"(memnos recall unavailable: {e})"


@mcp.tool()
def remember(text: str) -> str:
    """Save a durable fact, preference, decision, or piece of context to the user's
    long-term memory for future sessions. Use for things worth keeping (preferences,
    project facts, commitments, identity) — not transient chatter. If this updates a
    prior fact (e.g. a changed preference), memnos supersedes the old value automatically."""
    ns, source = _ns_source()
    try:
        # async:true — server stores the raw turn immediately and extracts facts in the
        # background, so a slow local-LLM extraction backend (Ollama 30-80s) can't ReadTimeout
        # and drop the write. The raw turn is durable the moment this returns.
        out = _post("/remember", {"text": text, "speaker": "user", "async": True})
    except Exception as e:
        # Bug 4: raise so the MCP result is flagged isError=true — never a false "saved".
        raise ToolError(_write_error(e, "remember")) from None
    # write-time attribution (issue #20, Part B): always name the destination namespace so
    # the chat client relays WHERE the memory landed.
    dest = out.get("namespace") or ns
    if out.get("extraction") == "queued":
        msg = f"remembered in '{dest}' (turn {out.get('turn_id')}; facts extracting in background)"
    else:
        msg = f"remembered in '{dest}' (turn {out.get('turn_id')}, {out.get('facts', 0)} facts extracted)"
    # default-fallback: no binding for this repo — surface the one-step bind offer.
    if source == "default":
        try:
            import nsresolve
            msg += "\n" + nsresolve.default_fallback_hint(dest)
        except Exception:
            pass
    sugg = out.get("suggestion")              # advisory only — the write already landed in `dest`
    if isinstance(sugg, dict) and sugg.get("namespace"):
        msg += (f"\nhint: this looks like '{sugg['namespace']}' ({sugg.get('reason','')}) — "
                f"bind future writes there if so.")
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
    try:
        out = _post("/memory/write", {"text": text, "speaker": "user"})
    except Exception as e:
        raise ToolError(_write_error(e, "memory_write")) from None
    return f"written (turn {out.get('turn_id')}, {out.get('facts', 0)} facts)"


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
def corpus_list() -> str:
    """List the architecture documents ingested into this namespace and their constraint counts."""
    try:
        return str(_post("/corpus/list", {}).get("sources", []))
    except Exception as e:
        return _err(e, "corpus_list")


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
    'mr:!51', 'repo:gridops-platform'). Returns granted=true if you now hold the lease;
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


if __name__ == "__main__":
    mcp.run()        # stdio transport (JSON-RPC over stdin/stdout)
