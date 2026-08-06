"""B1 regression: a failure inside the ADDITIVE HTTP-MCP mount (issue #37 Layer 1) must
degrade gracefully — :8900 keeps serving REST + the stdio adapter — never take the whole
server down. Before the fix, `serve()` called `start_mcp_http_mount(port)` unconditionally
with no try/except, directly before `ThreadingHTTPServer(...).serve_forever()`; any of its
four raise paths (bad `MEMNOS_MCP_INTERNAL_PORT`, port-in-use, mount-thread death, 15s
startup timeout) crashed the entire process — REST and stdio included — contradicting the
PR's own "purely additive" claim.

This test forces the simplest of the four raise paths deterministically — a malformed
`MEMNOS_MCP_INTERNAL_PORT` env var, which raises `ValueError` inside `int(env_port)` before
any socket/thread work happens, so it's fast and has zero port-collision flake in CI.
(Any of the other three raise paths would exercise the exact same `serve()`-level guard;
this one was picked for determinism, not because it's special.)

Owns its own memnos_server.py subprocess on a dedicated port (never touches a real :8900
instance). Verifies, against the forced failure:
  1. the server still boots to /readyz despite the mount failing
  2. the failure is logged clearly as the MCP-HTTP mount (not a fatal server crash)
  3. REST (/healthz) is fully functional
  4. the stdio adapter is fully functional — a REAL subprocess, REAL JSON-RPC round trip
     (same rigor as test_mcp_stdio_transport.py), not a `.fn` direct call
  5. /mcp itself answers a clean 503 (mount never started) instead of hanging or crashing

Run: python tests/test_mcp_http_mount_failure.py
(spawns its own server; does not require one already running)
"""
import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anyio
import httpx
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_MOUNT_FAILURE_TEST_PORT", "8956"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:mcp-mount-failure"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMNOS_MCP_PY = os.path.join(ROOT, "memnos_mcp.py")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _server_env():
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT),
              MEMNOS_MCP_INTERNAL_PORT="notaport")   # forces int() ValueError in start_mcp_http_mount
    env.setdefault("OPENAI_API_KEY", "")     # force free local-384 embeddings (no vault/network dependency)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return env


def start_server(log_path):
    logf = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "memnos_server.py")],
                            cwd=ROOT, env=_server_env(), stdout=logf, stderr=subprocess.STDOUT)
    logf.close()
    ready = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = httpx.get(URL + "/readyz", timeout=1)
            if r.status_code == 200 and r.json().get("ready"):
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    return proc, ready


def stop_server(proc, timeout=15):
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def cleanup_db(conn):
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                  "WHERE t.principal_id=pr.id AND pr.name=%s", ("mcpmountfail_agent",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name=%s", ("mcpmountfail_agent",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("mcpmountfail_agent",))


async def _stdio_round_trip(token, ns, text, query):
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
    Control.init(conn)   # control-plane only (principals/grants/tokens) — safe pre-boot

    log_path = tempfile.mktemp(prefix="memnos_mount_failure_", suffix=".log")
    print(f"=== starting server subprocess on {URL} with MEMNOS_MCP_INTERNAL_PORT=notaport "
          f"(forces start_mcp_http_mount's int() ValueError raise path) ===")
    proc, ready = start_server(log_path)
    log = open(log_path).read() if os.path.exists(log_path) else ""

    check("server still reaches /readyz despite the HTTP-MCP mount failing to start",
          ready, f"process exited early (code {proc.returncode})" if proc.poll() is not None
                 else "timed out waiting for /readyz")
    check("failure is logged clearly as the MCP-HTTP mount (not a silent or fatal crash)",
          "MCP-HTTP-mount" in log and "WARNING" in log, log[-600:])

    if not ready:
        # Nothing further can be meaningfully asserted — report and bail. The memory
        # schema (tenant_memnos.*) may not exist yet on a from-scratch DB since only a
        # successful boot creates it — skip cleanup_db rather than fail on a missing table.
        if proc.poll() is None:
            stop_server(proc)
        os.remove(log_path) if os.path.exists(log_path) else None
        print(f"\n{PASS} passed, {FAIL} failed")
        sys.exit(1)

    # Server booted (schema now exists, created during boot before the mount attempt) —
    # safe to clean up and mint a token.
    cleanup_db(conn)
    try:
        pid = Control.create_principal(conn, "mcpmountfail_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcpmountfail_agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "mcpmountfail-test")

    print("=== REST is fully functional despite the mount failure ===")
    r = httpx.get(URL + "/healthz", timeout=5)
    check("REST /healthz still responds 200", r.status_code == 200, str(r.status_code))

    print("=== stdio adapter is fully functional (real subprocess, real JSON-RPC) ===")
    try:
        remember_text, recall_text = anyio.run(
            _stdio_round_trip, token, NS,
            "The mount-failure resilience test constant is MF-7734.",
            "mount-failure resilience test constant")
        check("remember() over real stdio JSON-RPC still works", "remembered" in remember_text, remember_text)
        check("recall() over real stdio JSON-RPC still returns the content just written",
              "MF-7734" in recall_text, recall_text)
    except Exception as e:
        check("stdio round trip succeeded despite the HTTP-MCP mount failure",
              False, f"{type(e).__name__}: {e}")

    print("=== /mcp itself answers a clean 503, not a hang or crash ===")
    try:
        r2 = httpx.post(URL + "/mcp", timeout=10,
                        headers={"Authorization": "Bearer irrelevant-mount-is-down",
                                 "Content-Type": "application/json"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        check("/mcp returns 503 (mount unavailable) rather than hanging or 500ing",
              r2.status_code == 503, f"got {r2.status_code}: {r2.text[:200]}")
    except httpx.TransportError as e:
        check("/mcp returns 503 (mount unavailable) rather than hanging or 500ing",
              False, f"request itself failed: {type(e).__name__}: {e}")

    stop_server(proc)
    cleanup_db(conn)
    os.remove(log_path) if os.path.exists(log_path) else None
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
