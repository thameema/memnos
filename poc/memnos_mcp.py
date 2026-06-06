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


if __name__ == "__main__":
    mcp.run()        # stdio transport (JSON-RPC over stdin/stdout)
