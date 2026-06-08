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
import os
import httpx
from mcp.server.fastmcp import FastMCP

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900").rstrip("/")
TOKEN = os.environ.get("MEMNOS_TOKEN", "")
NS = os.environ.get("MEMNOS_NS", "claude:default")

mcp = FastMCP("memnos")


def _post(path, payload):
    r = httpx.post(f"{URL}{path}", json={"namespace": NS, **payload},
                   headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def recall(query: str) -> str:
    """Search the user's long-term memory for information relevant to the query.
    Returns remembered facts and statements (current values preferred over superseded
    ones). Call this whenever the user references past conversations, stated
    preferences, prior decisions, people/projects, or context not in this session."""
    try:
        ctx = _post("/recall", {"query": query}).get("context", "")
        return ctx or "(no relevant memories found)"
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
    try:
        out = _post("/remember", {"text": text, "speaker": "user"})
        return f"remembered (turn {out.get('turn_id')}, {out.get('facts', 0)} facts extracted)"
    except httpx.HTTPStatusError as e:
        return f"(memnos remember failed: HTTP {e.response.status_code})"
    except Exception as e:
        return f"(memnos remember failed: {e})"


@mcp.tool()
def consolidate() -> str:
    """Run memory consolidation (the offline 'sleep' pass): distill recent episodes
    into durable semantic facts and entity dossiers, resolving contradictions. Normally
    run on a schedule; expose here for on-demand use."""
    try:
        return f"consolidated: {_post('/consolidate', {})}"
    except Exception as e:
        return f"(memnos consolidate failed: {e})"


def _err(e, what):
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        if c == 401: return "(memnos: unauthorized — check MEMNOS_TOKEN)"
        if c == 403: return f"(memnos: not authorized for namespace {NS})"
        if c == 404: return f"(memnos: not found)"
        return f"(memnos {what} error: HTTP {c})"
    return f"(memnos {what} unavailable: {e})"


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
        return f"written (turn {out.get('turn_id')}, {out.get('facts', 0)} facts)"
    except Exception as e:
        return _err(e, "memory_write")


@mcp.tool()
def memory_delete(id: int) -> str:
    """Delete (system-time expire) a semantic fact by its numeric id. The fact is excluded
    from all future retrieval; history is preserved (never hard-deleted). Get ids from
    memory_search or get_entity."""
    try:
        out = _post("/memory/delete", {"id": int(id)})
        return f"deleted fact {out.get('deleted')}: {out.get('statement','')}"
    except Exception as e:
        return _err(e, "memory_delete")


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
def knowledge_health() -> str:
    """Return a knowledge-health report for this namespace: a 0-100 score plus signals
    (current/superseded/expired facts, entities, orphan entities, contradiction groups)."""
    try:
        return str(_post("/knowledge/health", {}))
    except Exception as e:
        return _err(e, "knowledge_health")


@mcp.tool()
def get_context(query: str) -> str:
    """Return a ready-to-paste context block for a query (same as recall) — no LLM at
    query time."""
    try:
        return _post("/memory/context", {"query": query}).get("context", "") or "(no relevant memories)"
    except Exception as e:
        return _err(e, "get_context")


if __name__ == "__main__":
    mcp.run()        # stdio transport (JSON-RPC over stdin/stdout)
