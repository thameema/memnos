"""issue #37 Layer 2 — TEST 3: draining the OLD backend must not drop an in-flight
request that started before the flip.

Acceptance bar (from the issue): "Drain the OLD instance: let its in-flight requests
complete, refuse new ones, then shut it down cleanly — don't hard-kill it while it might
still be serving a request."

Mechanism: seeds a namespace with enough distinct facts that a broad recall() has a real
candidate pool to rerank, then uses the reranker's OWN existing test hook
(MEMNOS_RERANK_SIMULATED_MS_PER_PAIR, issue #34 — not something added for this test) to
make that recall() call deterministically slow (several real seconds) WITHOUT touching
production code. A recall() is started on a background thread against the public gateway
port; once it is genuinely in flight (dispatched to the OLD backend, gateway's own
in-flight counter incremented), a real rolling upgrade is triggered. A second thread
polls the OLD backend's OS pid for liveness (os.kill(pid, 0)) throughout. The test then
proves, with real timestamps:
  1. the slow recall() completes successfully and returns the CORRECT content — it was
     never dropped, reset, or served an empty/error response mid-swap
  2. the OLD backend process was never torn down before that recall() actually finished
     (liveness sampling never observed it gone until at/after the recall's own completion
     time) — i.e. the gateway's drain wait genuinely gated the kill on in-flight
     completion, not on a fixed timer that happened to be long enough by luck

Run: python tests/test_zero_downtime_inflight_drain.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
CLI_PATH = os.path.join(ROOT, "memnos_cli.py")

BASE_DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
TEST_DB = "memnos_test_zero_downtime_drain"
PORT = int(os.environ.get("MEMNOS_ZDT_DRAIN_TEST_PORT", "8972"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:zero-downtime-drain"
N_SEED_FACTS = 20
MIN_EXPECTED_SLEEP_S = 2.0     # the slow recall must genuinely take a while, not be a fluke
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


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
    u = urlsplit(dsn)
    host, port = (u.hostname or "localhost"), (u.port or 5432)
    base_admin = dsn.rsplit("/", 1)[0] + "/postgres"
    os_user_admin = f"postgresql://{os.environ.get('USER', 'postgres')}@{host}:{port}/postgres"
    return [base_admin, os_user_admin]


def _test_env(home_dir, dsn):
    env = dict(os.environ)
    env["HOME"] = home_dir
    env["MEMNOS_DSN"] = dsn
    env["MEMNOS_PORT"] = str(PORT)
    env.setdefault("MEMNOS_SECRET_KEY", "Y2lfdGVzdF9rZXlfMzJfYnl0ZXNfZXhhY3RseV9vayE=")
    env["OPENAI_API_KEY"] = ""
    # issue #34's existing rerank test hooks — NOT new for this test — used here purely
    # to make a real recall() call deterministically slow so it's reliably still
    # in-flight when the upgrade is triggered.
    env["MEMNOS_RERANK_SIMULATED_MS_PER_PAIR"] = "400"
    env["MEMNOS_RERANK_MIN_CAP"] = "15"
    # deterministic $0 fact extraction (memnos_server.py's own existing test-only
    # extractor, gated by this env var) — populates real structured facts from the
    # seeded text so recall() has a genuine, non-trivial rerank candidate pool to work
    # with (a bare raw-text match alone isn't enough to reliably engage the reranker).
    env["MEMNOS_FAKE_EXTRACT"] = "1"
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def start_gateway(home_dir, dsn):
    proc = subprocess.Popen([PY, CLI_PATH, "gateway", "--port", str(PORT)],
                            cwd=ROOT, env=_test_env(home_dir, dsn),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    state_path = os.path.join(home_dir, ".memnos", "gateway_state.json")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gateway process exited early with code {proc.returncode}")
        if os.path.exists(state_path):
            try:
                import httpx
                r = httpx.get(URL + "/readyz", timeout=1)
                if r.status_code == 200 and r.json().get("ready"):
                    return proc, json.load(open(state_path))
            except Exception:
                pass
        time.sleep(0.3)
    proc.kill()
    raise RuntimeError(f"gateway did not become ready on {URL} within 300s")


def stop_gateway(proc, timeout=20):
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def gw_get(path, token, timeout=5):
    import httpx
    return httpx.get(URL + path, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)


def gw_post(path, token, body=None, timeout=60):
    import httpx
    return httpx.post(URL + path, headers={"Authorization": f"Bearer {token}"},
                      json=body or {}, timeout=timeout)


def wait_rerank_ready(token, timeout=40):
    """Block until the backend's OWN reranker background calibration (issues #12/#34,
    core/rerank.py's prewarm_background — separate from /readyz's pool+HNSW gate) has
    finished. A recall() issued while the reranker is still warming silently skips
    reranking (serves plain RRF order, `degraded: true` in the response) REGARDLESS of
    candidate count — so this test's timing only makes sense once that warm-up is
    actually done; /readyz alone doesn't guarantee it (by design — see /readyz's own
    docstring in memnos_server.py)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = gw_post("/recall", token, {"namespace": NS, "query": "reranker warmup probe"},
                       timeout=15)
            if r.status_code == 200 and not (r.json() or {}).get("degraded"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main():
    import psycopg
    from psycopg.rows import dict_row
    from core.control import Control

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
    dsn = with_dbname(BASE_DSN, TEST_DB)

    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    pid_ = Control.create_principal(conn, "zdt-drain-agent", "agent")
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "zdt-drain-test")

    home_dir = tempfile.mkdtemp(prefix="memnos_zdt_drain_home_")
    gw_proc = None
    try:
        print(f"=== starting gateway on {URL} (reranker simulated at 400ms/pair, cap>=15) ===")
        gw_proc, state = start_gateway(home_dir, dsn)
        ctl_token = state["control_token"]
        st0 = gw_get("/__gateway__/status", ctl_token).json()
        old_pid = st0.get("current_backend_pid")
        old_port = st0.get("current_backend_port")
        print(f"  gateway up — old backend pid={old_pid} internal_port={old_port}")
        check("gateway reports a live old backend", bool(old_pid), st0)

        rr_ready = wait_rerank_ready(token)
        check("the backend's reranker finished its own background calibration before "
              "the test proceeds (otherwise recall() silently skips reranking)", rr_ready)

        print(f"=== seeding {N_SEED_FACTS} facts for a real, non-trivial rerank candidate pool ===")
        for i in range(N_SEED_FACTS):
            r = gw_post("/remember", token, {"namespace": NS, "text":
                        f"Station Zephyr{i} of the Albatross Colony Survey recorded a "
                        f"Wingspan measurement of {200 + i} centimeters for Specimen "
                        f"Kestrel{i} near Halden Bay."})
            if r.status_code != 200:
                raise RuntimeError(f"seed remember #{i} failed: {r.status_code} {r.text}")

        recall_result = {}

        def slow_recall():
            t0 = time.monotonic()
            try:
                r = gw_post("/recall", token,
                           {"namespace": NS, "query": "Albatross Colony Survey wingspan Zephyr station"},
                           timeout=120)
                recall_result["status"] = r.status_code
                recall_result["body"] = r.json() if r.status_code == 200 else r.text
            except Exception as e:
                recall_result["exception"] = f"{type(e).__name__}: {e}"
            recall_result["t_start"] = t0
            recall_result["t_end"] = time.monotonic()

        old_pid_gone_at = {"t": None}
        stop_poll = threading.Event()

        def liveness_poll():
            while not stop_poll.is_set():
                if not pid_alive(old_pid):
                    if old_pid_gone_at["t"] is None:
                        old_pid_gone_at["t"] = time.monotonic()
                    return
                time.sleep(0.05)

        print("=== starting the slow in-flight recall() (targets the OLD backend) ===")
        th_recall = threading.Thread(target=slow_recall, daemon=True)
        th_recall.start()
        time.sleep(0.4)      # let it actually dispatch and land on the old backend

        # confirm it's genuinely counted as in-flight on the gateway before we flip
        st_inflight = gw_get("/__gateway__/status", ctl_token).json()
        in_flight_n = (st_inflight.get("inflight") or {}).get(str(old_port), 0)
        check("gateway's own in-flight counter shows the slow recall as in-flight on "
              "the OLD backend BEFORE the upgrade is triggered", in_flight_n >= 1,
              st_inflight.get("inflight"))

        th_live = threading.Thread(target=liveness_poll, daemon=True)
        th_live.start()

        print("=== triggering the rolling upgrade WHILE the slow recall is still running ===")
        r = gw_post("/__gateway__/upgrade", ctl_token)
        check("upgrade request accepted (202)", r.status_code == 202, r.text)

        up = {}
        deadline = time.monotonic() + 300
        last_phase = None
        while time.monotonic() < deadline:
            st = gw_get("/__gateway__/status", ctl_token).json()
            up = st.get("upgrade") or {}
            if up.get("phase") != last_phase:
                print(f"  · phase: {up.get('phase')}  (in-flight to old port: "
                     f"{(st.get('inflight') or {}).get(str(old_port), 0)})")
                last_phase = up.get("phase")
            if up.get("status") in ("done", "failed"):
                break
            time.sleep(0.1)

        th_recall.join(timeout=120)
        stop_poll.set()
        th_live.join(timeout=5)

        check("the rolling upgrade completed successfully", up.get("status") == "done", up)
        check("the old backend was reported drained (not force-killed on a bare timeout)",
              bool(up.get("drained")), up)

        check("the in-flight recall() did NOT raise/time out",
              "exception" not in recall_result, recall_result.get("exception"))
        check("the in-flight recall() returned HTTP 200",
              recall_result.get("status") == 200, recall_result.get("body"))
        dur = recall_result.get("t_end", 0) - recall_result.get("t_start", 0)
        check(f"the recall() genuinely took long enough to prove it was in-flight during "
              f"the swap (>= {MIN_EXPECTED_SLEEP_S}s, got {dur:.2f}s)",
              dur >= MIN_EXPECTED_SLEEP_S, f"{dur:.2f}s")
        ctx = ((recall_result.get("body") or {}).get("context", "")
               if isinstance(recall_result.get("body"), dict) else "")
        check("the in-flight recall() returned the CORRECT seeded content (not dropped, "
              "not a blank/degraded response)", "albatross" in ctx.lower(), ctx[:200])

        gone_at = old_pid_gone_at["t"]
        recall_end = recall_result.get("t_end")
        check("the OLD backend process was NEVER observed dead before the in-flight "
              "recall() actually finished (drain genuinely gated the kill on it)",
              gone_at is None or (recall_end is not None and gone_at >= recall_end - 0.25),
              f"old_pid_gone_at={gone_at} recall_end={recall_end}")

        st1 = gw_get("/__gateway__/status", ctl_token).json()
        check("a real backend swap happened (new pid differs from the old one)",
              st1.get("current_backend_pid") != old_pid,
              f"old={old_pid} new={st1.get('current_backend_pid')}")

    finally:
        if gw_proc is not None:
            stop_gateway(gw_proc)
        shutil.rmtree(home_dir, ignore_errors=True)
        try:
            maint = psycopg.connect(su_dsn if su_dsn else BASE_DSN, autocommit=True)
            maint.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            maint.close()
        except Exception:
            pass

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
