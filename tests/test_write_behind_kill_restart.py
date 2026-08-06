"""issue #37 Layer 3 — TEST 1 (the literal acceptance bar): kill the live memnos server
process mid-write; the write is queued (never lost, never written to a separate store);
restart the server; the queued write replays and becomes recallable from the SAME store.

Exercises the MCP adapter specifically (memnos_mcp.remember()/recall()) rather than the
Claude Code hooks — the hooks are Claude-Code-only, but Claude Desktop and omnigent (the
harnesses issue #37's "Generalization" section names) talk to memnos ONLY through this
adapter and never run a hook. So the adapter has to enqueue on failure and replay on its
OWN, with no SessionStart drain to lean on — this test proves exactly that loop, against
a REAL memnos_server.py subprocess (not a stub), on the local dev Postgres.

Isolation: both the spawned server and the MCP adapter run with HOME pointed at their own
temp dirs, so (a) the server never touches this machine's real ~/.memnos/config.json (no
real OpenAI key leaks in -> deterministic local-384 embeddings, $0), and (b) the adapter's
offline_queue never touches the real ~/.memnos/offline_queue/.

Run: python tests/test_write_behind_kill_restart.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
BASE_DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
# A DEDICATED, throwaway database (not the shared `memnos` db every other test's Postgres
# points at) — that shared db's vector column is pinned at 1536-d (the real OpenAI
# embedding size other tests run with). Our spawned server runs with an isolated HOME
# (no config.json -> no OpenAI key -> local-384 embeddings) for a deterministic, $0 test,
# so it needs its OWN 384-d schema; memnos_server.py auto-creates it on first connect.
TEST_DB = "memnos_test_write_behind"
SCHEMA = "tenant_memnos"
NS = "test:wb-kill-restart"
DISTINCTIVE_TEXT = "wb-kill-restart QUEUED FACT: the quokka summit relocated to Perth."
PASS = FAIL = 0


def with_dbname(dsn, dbname):
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{dbname}"


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(url, tries=90, interval=0.5):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=2)
            return True
        except Exception:
            time.sleep(interval)
    return False


def wait_down(url, tries=40, interval=0.25):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            time.sleep(interval)
        except Exception:
            return True
    return False


def snapshot_tree(root):
    out = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            out.add(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def start_server(server_home, dsn, port):
    env = dict(os.environ, HOME=server_home, MEMNOS_DSN=dsn, MEMNOS_PORT=str(port))
    env.pop("OPENAI_API_KEY", None)   # belt & suspenders — temp HOME has no config.json anyway
    logpath = os.path.join(server_home, "server.log")
    logf = open(logpath, "w")
    proc = subprocess.Popen([PY, os.path.join(ROOT, "memnos_server.py")], cwd=ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, logpath


def stop(proc, logf=None):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
    if logf:
        try:
            logf.close()
        except Exception:
            pass


def main():
    import psycopg
    from psycopg.rows import dict_row
    from core.control import Control

    # DB/extension creation needs superuser (pgvector's CREATE EXTENSION does); the local
    # OS user is trust-authenticated as Postgres superuser on this dev box, same as
    # `memnos setup` itself would prompt for. Everything AFTER this uses the ordinary
    # `memnos` role from BASE_DSN, same as every other test.
    su_dsn = f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/postgres"
    maint = psycopg.connect(su_dsn, autocommit=True)
    maint.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    maint.execute(f'CREATE DATABASE "{TEST_DB}" OWNER memnos')
    maint.close()
    maint_db = psycopg.connect(with_dbname(su_dsn, TEST_DB), autocommit=True)
    maint_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    maint_db.close()
    DSN = with_dbname(BASE_DSN, TEST_DB)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    try:
        pid = Control.create_principal(conn, "wb-kill-restart-agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s",
                      ("wb-kill-restart-agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "wb-kill-restart")
    # (fresh, dedicated database — no prior rows to clean; tenant_memnos doesn't exist
    # yet either, it's auto-created by the server's own create_schema() on first start)

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server_home = tempfile.mkdtemp(prefix="memnos_wb_srv_")
    client_home = tempfile.mkdtemp(prefix="memnos_wb_client_")
    proc = logf = None

    try:
        proc, logf, logpath = start_server(server_home, DSN, port)
        up = wait_up(url)
        check("test server came up (isolated HOME -> local-384 mode, deterministic)", up)
        if not up:
            print("---- server log tail ----")
            try:
                print(open(logpath).read()[-2000:])
            except Exception:
                pass
        else:
            os.environ["HOME"] = client_home
            os.environ["MEMNOS_URL"] = url
            os.environ["MEMNOS_TOKEN"] = token
            os.environ["MEMNOS_NS"] = NS
            import memnos_mcp
            remember = getattr(memnos_mcp.remember, "fn", memnos_mcp.remember)
            recall = getattr(memnos_mcp.recall, "fn", memnos_mcp.recall)

            before = snapshot_tree(client_home)

            r0 = remember("wb-kill-restart control fact: the sky is blue today.")
            check("control write while server is healthy: normal success (NOT queued)",
                  isinstance(r0, str) and "queued" not in r0.lower() and "remembered" in r0.lower())

            # --- KILL THE SERVER --------------------------------------------------------
            stop(proc, logf)
            check("server process actually exited after termination", proc.poll() is not None)
            check("server no longer answers healthz", wait_down(url))

            r1 = remember(DISTINCTIVE_TEXT)
            check("write against a DEAD server does NOT raise (no false 'FAILED — NOT saved')",
                  isinstance(r1, str))
            check("write against a DEAD server returns a QUEUED success message",
                  isinstance(r1, str) and "queued" in r1.lower())

            after_kill = snapshot_tree(client_home)
            new_files = after_kill - before
            check("exactly one new on-disk artifact was created by the queued write",
                  len(new_files) == 1)
            qpath = next(iter(new_files), "")
            qq_prefix = os.path.join(".memnos", "offline_queue") + os.sep
            check("that artifact lives under ~/.memnos/offline_queue/ — never a separate store",
                  qpath.startswith(qq_prefix) and qpath.endswith(".json"))
            check("no other new path appeared anywhere under the client home",
                  new_files == {qpath})
            try:
                with open(os.path.join(client_home, qpath)) as fh:
                    qitem = json.load(fh)
            except Exception:
                qitem = {}
            check("the queued item carries the EXACT text and the target namespace",
                  qitem.get("text") == DISTINCTIVE_TEXT and qitem.get("namespace") == NS)

            row = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                               f"WHERE namespace=%s AND text=%s", (NS, DISTINCTIVE_TEXT)).fetchone()
            check("the write is NOT yet in the store while the server is down (genuinely queued)",
                  row["n"] == 0)

            # --- RESTART THE SERVER (same DSN/port — same store) ------------------------
            proc, logf, logpath = start_server(server_home, DSN, port)
            up2 = wait_up(url)
            check("server came back up on restart (same DSN + port -> same store)", up2)

            drained_ok = False
            for _ in range(20):
                recall("quokka summit relocation")     # each call opportunistically drains
                row2 = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                                    f"WHERE namespace=%s AND text=%s",
                                    (NS, DISTINCTIVE_TEXT)).fetchone()
                if row2["n"] > 0:
                    drained_ok = True
                    break
                time.sleep(0.5)
            check("queued write REPLAYED into the SAME store after the server returned "
                  "(no session/client restart — the SAME adapter process drained it)",
                  drained_ok)
            check("the offline_queue file was removed after a successful replay",
                  not os.path.exists(os.path.join(client_home, qpath)))

            last = recall("quokka summit relocation")
            check("the queued write is RECALLABLE (not just a row — the real read path)",
                  "quokka" in last.lower() or "perth" in last.lower())
    finally:
        stop(proc, logf)
        conn.close()
        shutil.rmtree(server_home, ignore_errors=True)
        shutil.rmtree(client_home, ignore_errors=True)
        # drop the dedicated throwaway database (only after the server + our own
        # connection are both closed) — leaves nothing behind on the shared Postgres.
        try:
            su_dsn = f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/postgres"
            maint2 = psycopg.connect(su_dsn, autocommit=True)
            maint2.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint2.close()
        except Exception as e:
            print(f"(cleanup warning: could not drop {TEST_DB}: {e})")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
