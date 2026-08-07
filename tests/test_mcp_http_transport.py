"""Streamable-HTTP MCP transport (issue #37 Layer 1): a REAL MCP client, using the
official SDK's streamable-HTTP transport, connects to the endpoint mounted at
{MEMNOS_URL}/mcp and does an actual remember -> recall round trip through the protocol
(initialize, tools/call) — not a direct Python function call into memnos_mcp.py. This is
the rigor test_mcp_write_errors.py does NOT have (it calls the decorated tool's .fn
directly, bypassing the wire protocol entirely); this file exercises the real transport.

Also covers: missing/invalid Bearer token -> 401 at the transport edge (before any tool
call succeeds), and a token with no X-Memnos-Namespace header + more than one namespace
grant -> a clear 400 (namespace is required, never silently guessed wrong).

Run against a live local server:
    memnos start   (or python memnos_server.py)
    python tests/test_mcp_http_transport.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anyio
import httpx
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
MCP_URL = URL + "/mcp"
NS = "test:mcp-http-transport"
NS2 = "test:mcp-http-transport-2"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace IN (%s, %s)", (NS, NS2))
        c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace IN (%s, %s)", (NS, NS2))
        for p in ("mcphttp_agent", "mcphttp_multi_agent"):
            c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                      "WHERE t.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                      "WHERE g.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.principals WHERE name=%s", (p,))


async def _round_trip(token, ns, text, query):
    """Real MCP protocol traffic: streamable-HTTP transport, initialize, two tool calls."""
    async with streamablehttp_client(
        MCP_URL, headers={"Authorization": f"Bearer {token}", "X-Memnos-Namespace": ns},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            remember_result = await session.call_tool("remember", {"text": text})
            recall_result = await session.call_tool("recall", {"query": query})
            return remember_result.content[0].text, recall_result.content[0].text


async def _edge_probe(headers, expect_status):
    """A bare POST (no MCP client) so we can assert the raw HTTP status the transport
    edge returns BEFORE any MCP session logic runs — 401/400 must reject at the door."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(MCP_URL, headers={**headers, "Content-Type": "application/json",
                                           "Accept": "application/json, text/event-stream"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}})
        return r.status_code


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    # single-namespace principal: exercises the explicit X-Memnos-Namespace header path
    try:
        pid = Control.create_principal(conn, "mcphttp_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcphttp_agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "mcphttp-test")

    # multi-namespace principal: exercises the "no header, ambiguous -> 400" path
    try:
        pid2 = Control.create_principal(conn, "mcphttp_multi_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcphttp_multi_agent",))
            pid2 = c.fetchone()["id"]
    Control.grant(conn, pid2, NS)
    Control.grant(conn, pid2, NS2)
    multi_token = Control.mint_token(conn, pid2, "mcphttp-multi-test")

    print("=== real streamable-HTTP MCP client: remember -> recall round trip ===")
    remember_text = "The streamable-HTTP MCP transport test constant is QZ-4471."
    remember_out, recall_out = anyio.run(_round_trip, token, NS, remember_text,
                                         "streamable-HTTP MCP transport test constant")
    check("remember() over streamable-HTTP returns a normal success string",
          "remembered" in remember_out, remember_out)
    check("recall() over streamable-HTTP returns the SAME content just written",
          "QZ-4471" in recall_out, recall_out)

    print("=== edge auth: transport-level rejection before any tool call ===")
    status_no_token = anyio.run(_edge_probe, {}, 401)
    check("no Authorization header -> 401 at the transport edge", status_no_token == 401,
          f"got {status_no_token}")
    status_bad_token = anyio.run(_edge_probe, {"Authorization": "Bearer not-a-real-token"}, 401)
    check("invalid Bearer token -> 401 at the transport edge", status_bad_token == 401,
          f"got {status_bad_token}")
    status_no_ns = anyio.run(_edge_probe, {"Authorization": f"Bearer {multi_token}"}, 400)
    check("valid token, multiple namespace grants, NO X-Memnos-Namespace header -> 400 "
          "(never silently guesses which namespace)", status_no_ns == 400, f"got {status_no_ns}")

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
