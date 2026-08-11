"""Cold-start readiness regression (issues #31/#59): a genuine memnos_server.py restart
must not report itself ready for traffic before the pool + HNSW vector indexes are
actually responsive, and a transient DB failure during that window must not silently
drop async ingest's derived facts.

Real subprocess, real Postgres, real SIGTERM + relaunch on the same port — the pattern
tests/test_mcp_http_restart_resilience.py (issue #37/#38) established: prove behavior
against an ACTUAL restart, not a design assertion.

Reproducing a literal disk-cache-cold HNSW scan needs real production scale/timing (issue
#31's own field data: 143K facts, 20-60s) that isn't reproducible on demand in a test
process. Instead, both scenarios below force the exact OBSERVABLE failure shape a cold
index produces — a real psycopg.errors.QueryCanceled — via an ACCESS EXCLUSIVE lock held
across the relevant window, the SAME technique tests/test_recall_arm_degrade_http.py
already uses for the identical class of failure. That is what the code under test (the
boot-time warm_indexes() gate, /readyz, and _ingest_worker's retry) actually reacts to;
simulating real disk I/O latency is not this test's job.

  SCENARIO A — a real restart's boot-time gate: start server A normally, confirm a
    baseline recall works, SIGTERM it, then relaunch server B on the SAME port with an
    ACCESS EXCLUSIVE lock held on tenant_memnos.raw_turns across its ENTIRE boot. Proves
    the port doesn't even accept a TCP connection (not "up but degraded" — genuinely not
    listening yet) until the lock releases and warm_indexes() actually completes, /readyz
    then reports ready promptly, server B has a different pid than A (a real restart, not
    a no-op), and the very first real /recall against the freshly-restarted server is NOT
    degraded.

  SCENARIO B — ingest survives the cold window: once the server is up, lock
    tenant_memnos.semantic (not raw_turns — P1b's raw-turn write must succeed, only the
    downstream extraction write should fail) and send a real async /remember. The first
    write_facts() attempt hits a real statement_timeout cancellation; issue #31's retry
    path re-attempts against the SAME surviving raw_turn once the lock releases, and both
    the raw turn AND its derived fact end up durably present — nothing silently dropped,
    and the audit ledger never records a permanent failure for this turn.

Run: python tests/test_coldstart_readiness_gate.py
(spawns its own server; does not require one already running)
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_COLDSTART_TEST_PORT", "8966"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:coldstart-readiness"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _server_env():
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT))
    env["OPENAI_API_KEY"] = ""              # force free local-384 embeddings
    env.setdefault("MEMNOS_SECRET_KEY", "Y29sZHN0YXJ0X3Rlc3Rfa2V5XzMyYl9leGFjdGx5X29rXyE=")
    env["MEMNOS_FAKE_EXTRACT"] = "1"        # deterministic $0 extractor (see memnos_server.py)
    # tight but not pathological — a genuine QueryCanceled needs to fire well inside the
    # few-second lock windows below, without flaking on an ordinarily-loaded CI runner.
    env["MEMNOS_STMT_TIMEOUT_MS"] = "2000"
    env["MEMNOS_INGEST_RETRY_BASE_S"] = "1"
    env["MEMNOS_INGEST_RETRY_MAX_S"] = "2"
    return env


_SERVER_LOGS = []  # (label, proc, log_path) — diagnostic dump on failure, see _dump_server_logs


def start_server_locked(label="server"):
    """Launch memnos_server.py and return the Popen handle immediately — does NOT wait
    for readiness (the caller controls a lock that determines when boot can finish).
    stdout/stderr go to a temp file (not DEVNULL) so a failing scenario can print the
    server's own boot/degrade log lines (e.g. "rerank calibrated: ..." / "recall arm
    degraded: ...") instead of silently discarding the diagnostic that explains why."""
    fd, path = tempfile.mkstemp(prefix=f"coldstart_{label.replace(' ', '_')}_", suffix=".log")
    log = os.fdopen(fd, "w")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "memnos_server.py")],
                            cwd=ROOT, env=_server_env(),
                            stdout=log, stderr=subprocess.STDOUT)
    log.close()  # child has its own dup'd fd; safe to close the parent's handle now
    _SERVER_LOGS.append((label, proc, path))
    return proc


def _dump_server_logs():
    for label, proc, path in _SERVER_LOGS:
        print(f"\n--- {label} (pid={proc.pid}) stdout+stderr ---")
        try:
            with open(path) as f:
                sys.stdout.write(f.read())
        except OSError as e:
            print(f"  (could not read log at {path}: {e})")


def _cleanup_server_logs():
    for _, _, path in _SERVER_LOGS:
        try:
            os.unlink(path)
        except OSError:
            pass


def port_accepts_connection(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(timeout_s=30):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = urllib.request.urlopen(URL + "/readyz", timeout=1)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def stop_server(proc, timeout=15):
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def call(path, token, body, timeout=15):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cleanup(conn):
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                  "WHERE t.principal_id=pr.id AND pr.name=%s", ("coldstart_gate_agent",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name=%s", ("coldstart_gate_agent",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("coldstart_gate_agent",))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    BrainStore(conn=conn).create_schema("memnos", dim=384)
    cleanup(conn)
    try:
        pid_ = Control.create_principal(conn, "coldstart_gate_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("coldstart_gate_agent",))
            pid_ = c.fetchone()["id"]
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "coldstart-gate-test")

    print("=== SCENARIO A: a genuine restart's readiness gate under a real lock ===")
    print("  starting server A normally first (same #38 pattern: prove a REAL restart, "
          "not just a single cold boot)")
    proc_a = start_server_locked("server A")
    check("server A comes up normally with no lock in the way", wait_ready(timeout_s=30))
    s, j = call("/recall", token, {"namespace": NS, "query": "baseline"})
    # degraded_reasons (not the bare `degraded` flag): `degraded` also flips True while
    # the reranker's own background prewarm is still loading right after boot (a
    # completely orthogonal, expected cause — recall_prefetch/recall_fetch never set
    # degraded_reasons for it) — that's issue #34's OWN design (serve fast-but-degraded
    # rather than block on the model load), not an index/pool arm failure, which is
    # what this scenario is actually about. Checking the bare flag here is what made
    # this same class of assertion an undiagnosed CI flake in
    # test_recall_arm_degrade_http.py (see PR #58's history) before this fix.
    check("server A's baseline recall is 200 and has no ARM failures",
          s == 200 and not j.get("degraded_reasons"), f"{s}: {j}")
    pid_a = proc_a.pid
    stop_server(proc_a)
    check("server A actually exited on SIGTERM", proc_a.poll() is not None)

    locker = psycopg.connect(DSN, autocommit=False, row_factory=dict_row)
    with locker.cursor() as lc:
        lc.execute("LOCK TABLE tenant_memnos.raw_turns IN ACCESS EXCLUSIVE MODE")
    print("  raw_turns locked — relaunching server B on the SAME port, expecting it to "
          "NOT open its port until the lock releases")

    proc = start_server_locked("server B")
    try:
        try:
            # give the process a moment to run past import/argv parsing, then confirm the
            # port genuinely refuses connections while boot is blocked on the lock —
            # "ready" and "port is open at all" are the same event now, not a race a
            # client can lose in between.
            time.sleep(1.5)
            check("server B's port is NOT open while boot is blocked on a cold index "
                  "(genuinely not listening, not merely 'up but unready')",
                  not port_accepts_connection(PORT),
                  f"pid={proc.pid} still alive={proc.poll() is None}")
            check("server B process is still alive (blocked, not crashed)", proc.poll() is None)

            locker.rollback()
            print("  lock released — expecting the port to open and /readyz to go ready promptly")
            ready = wait_ready(timeout_s=30)
            check("server B becomes ready shortly after the lock releases", ready)
            check("server B has a DIFFERENT pid than server A (a real restart, not a no-op)",
                  proc.pid != pid_a, f"A={pid_a} B={proc.pid}")

            s, j = call("/recall", token, {"namespace": NS, "query": "anything"})
            check("the FIRST real recall against the freshly-restarted server is 200",
                  s == 200, f"{s}: {j}")
            # degraded_reasons, not the bare flag — see the identical note on server A's
            # baseline check above.
            check("the first real recall has no ARM failures (warm_indexes already ran "
                  "during boot)", not j.get("degraded_reasons"), str(j))
        finally:
            locker.close()

        print("=== SCENARIO B: async ingest survives a transient cold-start-style DB failure ===")
        locker2 = psycopg.connect(DSN, autocommit=False, row_factory=dict_row)
        try:
            with locker2.cursor() as lc:
                # semantic, NOT raw_turns — the raw turn (P1b) must still land
                # synchronously; only the downstream extraction write (write_facts ->
                # semantic) should fail.
                lc.execute("LOCK TABLE tenant_memnos.semantic IN ACCESS EXCLUSIVE MODE")
            print("  semantic locked — sending a real async /remember")

            def release_after(delay):
                time.sleep(delay)
                locker2.rollback()

            threading.Thread(target=release_after, args=(2.5,), daemon=True).start()

            t0 = time.time()
            s, j = call("/remember", token,
                       {"namespace": NS, "text": "The cold-start regression constant is CSR-7741.",
                        "speaker": "user", "async": True})
            check("async /remember still returns 200 'queued' despite the lock ahead of it",
                  s == 200 and j.get("extraction") == "queued", f"{s}: {j}")
            turn_id = j.get("turn_id")

            # the raw turn must be durably present IMMEDIATELY — P1b never touches the
            # locked table, so this must not be delayed by the lock at all.
            with conn.cursor() as c:
                c.execute("SELECT count(*) AS n FROM tenant_memnos.raw_turns "
                          "WHERE id=%s AND namespace=%s", (turn_id, NS))
                raw_present = c.fetchone()["n"] == 1
            check("the raw turn is durably stored immediately, unaffected by the "
                  "semantic-table lock", raw_present)

    # NOTE: a successful async-ingest audit entry (memnos_server.py's _ingest_worker)
    # does NOT carry turn_id in its detail — only the failure/drop path does (see
    # _log_ingest_drop) — so this polls the LATEST 'remember' audit row for NS rather
    # than filtering by turn_id; only one /remember call happens in this scenario, so
            # the latest row is unambiguously this one.
            deadline = time.time() + 20
            outcome = None
            while time.time() < deadline:
                with conn.cursor() as c:
                    c.execute(
                        "SELECT ok, detail FROM memnos_control.audit_log "
                        "WHERE namespace=%s AND action='remember' "
                        "AND (detail->>'async')::boolean=true ORDER BY ts DESC LIMIT 1", (NS,))
                    row = c.fetchone()
                if row is not None:
                    outcome = row
                    if row["ok"]:
                        break
                time.sleep(0.3)
        finally:
            locker2.close()

        check("async ingest EVENTUALLY succeeds once the lock releases (retried, not dropped)",
              outcome is not None and outcome["ok"] is True, str(outcome))
        check("the successful audit entry reports real extracted facts, not a degraded no-op",
              bool(outcome) and (outcome.get("detail") or {}).get("facts", 0) > 0, str(outcome))

        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))
            fact_present = c.fetchone()["n"] > 0
        check("the derived fact is durably present in semantic — nothing silently lost",
              fact_present)

        elapsed = time.time() - t0
        print(f"  (scenario B took {elapsed:.1f}s wall clock — all of it off the client's "
              f"request path, which already got its 200 immediately)")
    finally:
        stop_server(proc)

    cleanup(conn)
    if FAIL:
        _dump_server_logs()
    _cleanup_server_logs()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
