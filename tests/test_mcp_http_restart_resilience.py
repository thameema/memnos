"""Restart resilience over streamable-HTTP (issue #37 Layer 1's actual acceptance bar):
a memnos server restart/upgrade must NOT force a client-side MCP session restart.

This owns its OWN memnos_server.py subprocess (a dedicated port, never the machine's
real :8900 instance) so it can kill -TERM and relaunch it mid-test. Mechanism, concretely:
  1. start server subprocess A, capture its PID
  2. open ONE real streamable-HTTP MCP client session (streamablehttp_client + ClientSession)
  3. call remember() over that session
  4. SIGTERM subprocess A, wait for exit, start subprocess B on the SAME port, poll /readyz,
     assert the PID actually changed (a same-PID "restart" would prove nothing)
  5. call recall() on the SAME session object from step 2 — NOT a newly-opened one — and
     assert it succeeds and returns the content written in step 3

stateless_http=True means the server keeps no session state to lose across a restart, so
this is expected to just work — but this test proves it against a REAL restart of a REAL
subprocess, not an assertion about the design. Any client-side error on the post-restart
call is a genuine finding and must surface as a FAILURE here, not be papered over with a
retry loop.

Run: python tests/test_mcp_http_restart_resilience.py
(spawns its own server; does not require one already running)
"""
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anyio
import httpx
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_RESTART_TEST_PORT", "8955"))
URL = f"http://127.0.0.1:{PORT}"
MCP_URL = URL + "/mcp"
NS = "test:mcp-http-restart"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _server_env():
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT))
    env.setdefault("OPENAI_API_KEY", "")     # force free local-384 embeddings (no vault/network dependency)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return env


def start_server():
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "memnos_server.py")],
                            cwd=ROOT, env=_server_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server process exited early with code {proc.returncode}")
        try:
            r = httpx.get(URL + "/readyz", timeout=1)
            if r.status_code == 200 and r.json().get("ready"):
                return proc
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise RuntimeError(f"server did not become ready on {URL} within 60s")


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
                  "WHERE t.principal_id=pr.id AND pr.name=%s", ("mcphttp_restart_agent",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name=%s", ("mcphttp_restart_agent",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("mcphttp_restart_agent",))


async def _run_with_open_session(token, before_restart, do_restart):
    """The whole point: ONE streamablehttp_client + ClientSession pair, opened before the
    restart and still open (not re-entered) when the post-restart call happens."""
    async with streamablehttp_client(
        MCP_URL, headers={"Authorization": f"Bearer {token}", "X-Memnos-Namespace": NS},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            remember_result = await session.call_tool("remember", before_restart)
            remember_text = remember_result.content[0].text

            # Restart happens HERE, mid-function, with the session object still alive and
            # in scope — this is the "no client-side session restart" acceptance bar.
            await anyio.to_thread.run_sync(do_restart)

            recall_result = await session.call_tool("recall", {"query": "restart resilience test constant"})
            return remember_text, recall_result.content[0].text


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup_db(conn)
    try:
        pid_ = Control.create_principal(conn, "mcphttp_restart_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("mcphttp_restart_agent",))
            pid_ = c.fetchone()["id"]
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "mcphttp-restart-test")

    print(f"=== starting server subprocess A on {URL} ===")
    proc_a = start_server()
    pid_a = proc_a.pid
    print(f"  server A up, pid={pid_a}")

    proc_b_holder = {}

    def do_restart():
        print("  SIGTERM -> server A ...")
        stop_server(proc_a)
        check("server A actually exited", proc_a.poll() is not None)
        print("  starting server B on the same port ...")
        proc_b = start_server()
        proc_b_holder["proc"] = proc_b
        print(f"  server B up, pid={proc_b.pid}")

    try:
        remember_text, recall_text = anyio.run(
            _run_with_open_session, token,
            {"text": "The restart resilience test constant is RR-8823."}, do_restart)
    except Exception as e:
        # Per the task: a real client-side error here is a FINDING, not something to
        # retry past silently. Report it as a failed check with the real exception.
        check("post-restart tool call on the SAME client session succeeded",
              False, f"{type(e).__name__}: {e}")
        remember_text = recall_text = ""
    else:
        check("remember() before the restart returns a normal success string",
              "remembered" in remember_text, remember_text)
        check("recall() AFTER the restart, on the SAME (never-recreated) client session, "
              "succeeds and returns the content written before the restart",
              "RR-8823" in recall_text, recall_text)

    proc_b = proc_b_holder.get("proc")
    check("server B has a DIFFERENT pid than server A (a real restart happened, not a no-op)",
          bool(proc_b) and proc_b.pid != pid_a, f"A={pid_a} B={proc_b.pid if proc_b else None}")

    if proc_b:
        stop_server(proc_b)
    if proc_a.poll() is None:
        stop_server(proc_a)
    cleanup_db(conn)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
