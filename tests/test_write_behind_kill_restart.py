"""issue #37 Layer 3 — TEST 1: kill the live memnos server process; the write is queued
or otherwise not lost (never silently dropped, never written to a separate store);
restart the server; the write becomes recallable from the SAME store.

Exercises the MCP adapter specifically (memnos_mcp.remember()/recall()) rather than the
Claude Code hooks — the hooks are Claude-Code-only, but Claude Desktop and omnigent (the
harnesses issue #37's "Generalization" section names) talk to memnos ONLY through this
adapter and never run a hook. So the adapter has to enqueue on failure and replay on its
OWN, with no SessionStart drain to lean on — this test proves exactly that loop, against
a REAL memnos_server.py subprocess (not a stub), on the local dev Postgres.

Two distinct kill scenarios, both against the real server:
  SCENARIO A — write attempted AFTER the server is confirmed fully down (`wait_down()`
    polls /healthz to completion first). This is "write during an outage", not a kill
    caught in flight — see SCENARIO B below for that.
  SCENARIO B — the literal "mid-write kill" bar: a write is fired on a background thread
    and the server process is torn down essentially concurrently with that request (no
    prior confirmation of shutdown, no artificial delay inserted into the server's
    request handling — this races real thread scheduling and real signal delivery
    against a real in-flight HTTP request/response). Because the exact instant of
    interruption isn't controllable without adding a test-only hook to production
    request handling, the assertions accept either legitimate outcome — the request
    completing successfully just before the kill lands, or a connection-level failure
    that gets queued — and treat only "unhandled exception", "hangs", or "the write
    never shows up anywhere after restart" as failures. That's the actual acceptance
    bar: not lost, not silently dropped — not a specific classification of HOW it
    resolved, since that's inherently racy and not something a black-box client-side
    test can pin down without instrumenting the server.

(An earlier revision of this test and PR #40's own description both called SCENARIO A
"kill mid-write" — inaccurate; it never fires anything before the server is confirmed
fully down. SCENARIO B was added to actually cover the claim.)

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
import threading
import time
import urllib.request
from urllib.parse import urlsplit

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
MID_FLIGHT_TEXT = "wb-kill-restart MID-FLIGHT FACT: the puffin colony moved to Reykjavik."
PASS = FAIL = 0


def with_dbname(dsn, dbname):
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{dbname}"


def redacted(dsn):
    u = urlsplit(dsn)
    netloc = u.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{host}"
    return u._replace(netloc=netloc).geturl()


def admin_dsn_candidates(dsn):
    """Two ways to reach a Postgres role that can CREATE DATABASE / CREATE EXTENSION,
    on the SAME host:port as BASE_DSN (never hardcoded — CI's Postgres service isn't
    on the default 5432). Which one has the needed privilege differs by environment:
    (1) BASE_DSN's own role against the 'postgres' maintenance db — this is the role
        CI's docker Postgres service grants superuser to (POSTGRES_USER=memnos), same
        pattern memnos_cli.py's cmd_setup uses to bootstrap a missing database.
    (2) OS-user trust auth — covers a native local Postgres install where the BASE_DSN
        role commonly has CREATEDB but not the superuser CREATE EXTENSION needs.
    """
    u = urlsplit(dsn)
    host, port = (u.hostname or "localhost"), (u.port or 5432)
    base_admin = dsn.rsplit("/", 1)[0] + "/postgres"
    os_user_admin = f"postgresql://{os.environ.get('USER', 'postgres')}@{host}:{port}/postgres"
    return [base_admin, os_user_admin]


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

    # DB/extension creation needs superuser (pgvector's CREATE EXTENSION does). Which role
    # has that privilege differs by environment — see admin_dsn_candidates() — so try each
    # candidate in turn and use whichever actually has it. Everything AFTER this uses the
    # ordinary role from BASE_DSN, same as every other test.
    owner = urlsplit(BASE_DSN).username or "memnos"
    su_dsn = None
    errors = []
    for candidate in admin_dsn_candidates(BASE_DSN):
        try:
            maint = psycopg.connect(candidate, autocommit=True)
            maint.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint.execute(f'CREATE DATABASE "{TEST_DB}" OWNER {owner}')
            maint.close()
            maint_db = psycopg.connect(with_dbname(candidate, TEST_DB), autocommit=True)
            maint_db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            maint_db.close()
            su_dsn = candidate
            break
        except Exception as e:
            errors.append(f"{redacted(candidate)}: {e}")
    if su_dsn is None:
        raise RuntimeError("could not bootstrap the test database via any admin DSN "
                            "candidate:\n  " + "\n  ".join(errors))
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
        # Cold start: create_schema() on a brand-new DB + first model load. ci.yml's own
        # server-start step budgets up to 5 min for this exact case ("an uncached run
        # downloads the models") — match it here rather than the 45s default, which is
        # tuned for the warm restart below.
        up = wait_up(url, tries=150, interval=2)
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

            # --- SCENARIO A: kill, CONFIRM fully down, then write ------------------------
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

            # --- SCENARIO B: kill CONCURRENTLY with an in-flight write --------------------
            # No wait_down() gate this time — the server (restarted above, currently healthy)
            # is torn down as close as possible to when the request is actually fired, racing
            # real thread scheduling and real signal delivery against a real in-flight HTTP
            # call. `about_to_call` is set on the request thread immediately before it calls
            # remember() (i.e. before the socket is even opened), and the main thread kills
            # the server the instant that fires — no sleep in between — to bias toward the
            # kill landing while the request is genuinely in flight rather than after a full
            # round trip. This can't be made fully deterministic without adding a test-only
            # delay hook to the server's own request handling (server registers no SIGTERM
            # handler either — see stop()/wait_down() above — so once the signal lands the
            # process dies immediately, no graceful in-flight completion to race against).
            about_to_call = threading.Event()
            outcome = {}

            def _mid_flight_write():
                about_to_call.set()
                try:
                    outcome["result"] = remember(MID_FLIGHT_TEXT)
                except Exception as e:
                    outcome["error"] = e

            t = threading.Thread(target=_mid_flight_write)
            t.start()
            check("mid-flight write thread signaled readiness before the kill",
                  about_to_call.wait(timeout=5))
            stop(proc, logf)                            # fires as soon as possible after the signal
            check("server process actually exited after the mid-flight kill",
                  proc.poll() is not None)
            t.join(timeout=20)
            check("mid-flight write call returned instead of hanging after the kill",
                  not t.is_alive())

            check("mid-flight write resolved to a normal string outcome, not an unhandled "
                  "exception (a connection reset mid-request must classify as transient, "
                  "never surface raw to the caller)",
                  "error" not in outcome and isinstance(outcome.get("result"), str))
            result = outcome.get("result") or ""
            # Either outcome is legitimate depending on exactly when the kill landed relative
            # to the server's request handling — a normal completed success (the response beat
            # the kill) or a queued-transient success (the connection was reset first). What
            # must NOT happen: a raised/permanent-shaped failure, or neither phrase present.
            check("mid-flight write outcome is a legitimate success shape "
                  "(completed OR queued — never a bare/unclassified failure)",
                  "remembered" in result.lower() or "queued" in result.lower())

            # --- RESTART AGAIN and confirm the mid-flight write is not lost --------------
            wait_down(url)                              # let the port free up before rebinding
            proc, logf, logpath = start_server(server_home, DSN, port)
            up3 = wait_up(url)
            check("server came back up after the mid-flight-kill restart", up3)

            mid_flight_landed = False
            for _ in range(20):
                recall("puffin colony Reykjavik")        # each call opportunistically drains
                row3 = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns "
                                    f"WHERE namespace=%s AND text=%s",
                                    (NS, MID_FLIGHT_TEXT)).fetchone()
                if row3["n"] > 0:
                    mid_flight_landed = True
                    break
                time.sleep(0.5)
            # Deliberately >= 1, not == 1: if the server had already committed the raw turn
            # before the kill severed the response, the client can't distinguish that from a
            # write that never landed at all — it correctly queues-and-replays either way,
            # which can legitimately produce one accepted duplicate (documented tradeoff in
            # offline_queue.is_transient()'s docstring: "accept the small risk of an eventual
            # duplicate turn, since losing the memory outright is worse"). The bar here is
            # "not lost", not "exactly-once" — this is the acceptance criterion SCENARIO B
            # exists to prove.
            check("mid-flight write is NOT LOST — it shows up in the store after restart "
                  "(queued-and-replayed, or already committed pre-kill — either is acceptable, "
                  "only silent loss is not)", mid_flight_landed)

            last2 = recall("puffin colony Reykjavik")
            check("the mid-flight write is RECALLABLE (real read path, not just a row)",
                  "puffin" in last2.lower() or "reykjavik" in last2.lower())
    finally:
        stop(proc, logf)
        conn.close()
        shutil.rmtree(server_home, ignore_errors=True)
        shutil.rmtree(client_home, ignore_errors=True)
        # drop the dedicated throwaway database (only after the server + our own
        # connection are both closed) — leaves nothing behind on the shared Postgres.
        # Reuse whichever admin DSN bootstrap actually worked with, rather than
        # re-guessing — the two candidates aren't equally valid in every environment.
        try:
            maint2 = psycopg.connect(su_dsn, autocommit=True)
            maint2.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint2.close()
        except Exception as e:
            print(f"(cleanup warning: could not drop {TEST_DB}: {e})")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
