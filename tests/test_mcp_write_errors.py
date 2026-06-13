"""Bug 4 contract: a FAILED MCP write must surface as an explicit ERROR to the model
(raised ToolError → MCP isError=true), NEVER a false "saved" string. Silent data loss on a
forbidden/failed write is trust-fatal: the agent would tell the user it remembered something
it never stored.

Runs against the live local server (MEMNOS_URL/MEMNOS_DSN). Mints a real principal that is
granted on namespace A but NOT on namespace B, points the MCP adapter at B, and asserts the
write tools RAISE rather than return success. Also asserts the happy path still returns a
normal success string. Run: python tests/test_mcp_write_errors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    # A scoped principal: granted ONLY on agent:mcptest_ok, NOT on agent:mcptest_forbidden.
    try:
        pid = Control.create_principal(conn, "mcptest_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcptest_agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, "agent:mcptest_ok")
    token = Control.mint_token(conn, pid, "mcptest")

    # Point the MCP adapter at the server with this token. Import AFTER setting env so the
    # module-level URL/TOKEN pick it up.
    os.environ["MEMNOS_URL"] = URL
    os.environ["MEMNOS_TOKEN"] = token
    os.environ["MEMNOS_NS"] = "agent:mcptest_forbidden"      # NOT granted → server returns 403
    import importlib
    import memnos_mcp
    importlib.reload(memnos_mcp)
    # MCP tools may be wrapped by the FastMCP decorator; call the underlying function.
    remember = getattr(memnos_mcp.remember, "fn", memnos_mcp.remember)
    memory_write = getattr(memnos_mcp.memory_write, "fn", memnos_mcp.memory_write)

    # 1. forbidden write RAISES (not a false success string)
    raised = False
    msg = ""
    try:
        out = remember("a distinctive durable fact the agent is trying to store here")
    except Exception as e:
        raised = True
        msg = str(e)
    check("forbidden remember(): RAISES an error (not a string return)", raised)
    check("forbidden remember(): error says FAILED / NOT saved (model can't misread as success)",
          raised and ("FAILED" in msg or "NOT saved" in msg) and "403" in msg)

    # 2. same for memory_write alias
    raised2 = False; msg2 = ""
    try:
        memory_write("another fact via the alias write path that should also be refused")
    except Exception as e:
        raised2 = True; msg2 = str(e)
    check("forbidden memory_write(): RAISES with FAILED/NOT saved", raised2 and "FAILED" in msg2)

    # 3. happy path still works: switch the adapter to the granted namespace.
    os.environ["MEMNOS_NS"] = "agent:mcptest_ok"
    importlib.reload(memnos_mcp)
    remember_ok = getattr(memnos_mcp.remember, "fn", memnos_mcp.remember)
    ok_result = None; ok_raised = False
    try:
        ok_result = remember_ok("Project Zephyr ships on the seventeenth of next month, confirmed.")
    except Exception as e:
        ok_raised = True; ok_result = str(e)
    check("granted remember(): returns a normal success string (no false negative)",
          (not ok_raised) and isinstance(ok_result, str) and "remembered" in ok_result)

    # cleanup
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace='agent:mcptest_ok'")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
