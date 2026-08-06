"""N1: stdio MCP transport — a REAL wire-protocol round trip, not a `.fn` direct call.

`memnos_mcp.py`'s FastMCP constructor gained `stateless_http=True, json_response=True`
(issue #37 Layer 1, for the streamable-HTTP mount) — kwargs that are documented as
HTTP-transport-only, but the stdio path (`mcp.run()`, JSON-RPC over stdin/stdout) had zero
test coverage that actually drives the wire protocol: `test_mcp_write_errors.py` calls the
decorated tool's `.fn` attribute directly, bypassing transport entirely, so it can't catch
a regression the constructor change might cause on the stdio side.

This test spawns `memnos_mcp.py` as a real subprocess (`mcp.client.stdio.stdio_client`),
speaks real JSON-RPC over its stdin/stdout, and does an actual remember -> recall round
trip through `ClientSession` — proving the FastMCP kwargs change left stdio intact.

Run against a live local server:
    memnos start   (or python memnos_server.py)
    python tests/test_mcp_stdio_transport.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anyio
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:mcp-stdio-transport"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMNOS_MCP_PY = os.path.join(ROOT, "memnos_mcp.py")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                  "WHERE t.principal_id=pr.id AND pr.name=%s", ("mcpstdio_agent",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name=%s", ("mcpstdio_agent",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("mcpstdio_agent",))


async def _round_trip(token, ns, text, query):
    """Real stdio subprocess, real JSON-RPC — the actual wire the FastMCP kwargs change
    could have broken, not a direct .fn() call into the module."""
    env = dict(os.environ, MEMNOS_URL=URL, MEMNOS_TOKEN=token, MEMNOS_NS=ns)
    params = StdioServerParameters(command=sys.executable, args=[MEMNOS_MCP_PY], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            remember_result = await session.call_tool("remember", {"text": text})
            recall_result = await session.call_tool("recall", {"query": query})
            return remember_result.content[0].text, recall_result.content[0].text


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    try:
        pid = Control.create_principal(conn, "mcpstdio_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcpstdio_agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "mcpstdio-test")

    print("=== real stdio MCP client (subprocess, real JSON-RPC): remember -> recall round trip ===")
    remember_text = "The stdio MCP transport test constant is SW-2290."
    remember_out, recall_out = anyio.run(_round_trip, token, NS, remember_text,
                                         "stdio MCP transport test constant")
    check("remember() over real stdio JSON-RPC returns a normal success string",
          "remembered" in remember_out, remember_out)
    check("recall() over real stdio JSON-RPC returns the SAME content just written",
          "SW-2290" in recall_out, recall_out)

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
