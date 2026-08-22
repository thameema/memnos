"""issue #37 Layer 2 — TEST 1: the actual zero-downtime acceptance bar.

Starts a REAL memnos_gateway.py process (issue #37 Layer 2's blue-green front door) on
its own dedicated port, backed by a REAL memnos_server.py backend it spawns itself. While
a background hammer continuously issues real HTTP requests against the PUBLIC port — each
one on a BRAND NEW TCP connection, not a reused keep-alive one, so a connection-refused
window can't hide behind connection pooling — this triggers a REAL rolling (blue-green)
upgrade via the gateway's own control endpoint (the same one `memnos restart`/`memnos
upgrade` use) and proves:

  1. the public port is never unreachable (zero connection refusals, the entire window)
  2. every single request served during the window succeeds (zero failures)
  3. a REAL backend swap actually happened (the backend pid before != the backend pid after
     — a same-pid "upgrade" would prove nothing)
  4. the swapped-to backend is fully functional (a real remember()+recall() round trip
     through the public port, after the swap, returns the content written before it)

This owns its own gateway + backend subprocesses (dedicated port, dedicated throwaway
database) — it never touches a real/shared memnos instance.

Run: python tests/test_zero_downtime_upgrade.py
"""
import http.client
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
TEST_DB = "memnos_test_zero_downtime_upgrade"
PORT = int(os.environ.get("MEMNOS_ZDT_TEST_PORT", "8970"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:zero-downtime-upgrade"
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
    """Same environment-portable bootstrap as tests/test_write_behind_kill_restart.py —
    see that file for the full rationale (CI's docker superuser vs. local trust auth)."""
    u = urlsplit(dsn)
    host, port = (u.hostname or "localhost"), (u.port or 5432)
    base_admin = dsn.rsplit("/", 1)[0] + "/postgres"
    os_user_admin = f"postgresql://{os.environ.get('USER', 'postgres')}@{host}:{port}/postgres"
    return [base_admin, os_user_admin]


def _test_env(home_dir, dsn, extra_path=None):
    """Isolated HOME (no real ~/.memnos/config.json leak — issue: it can carry a
    secret://-resolved DSN) + a PATH that deliberately EXCLUDES wherever a `memnos`
    console script might already be pip/uv-installed on this machine, so
    memnos_gateway.py's `shutil.which("memnos")` can't accidentally resolve to some
    OTHER installed version — every backend this test's gateway spawns is guaranteed to
    be running THIS checkout's memnos_server.py/memnos_cli.py."""
    env = dict(os.environ)
    env["HOME"] = home_dir
    env["MEMNOS_DSN"] = dsn
    env["MEMNOS_PORT"] = str(PORT)
    env.setdefault("MEMNOS_SECRET_KEY", "Y2lfdGVzdF9rZXlfMzJfYnl0ZXNfZXhhY3RseV9vayE=")
    env["OPENAI_API_KEY"] = ""
    env["PATH"] = (extra_path + os.pathsep if extra_path else "") + "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def start_gateway(home_dir, dsn):
    proc = subprocess.Popen([PY, CLI_PATH, "gateway", "--port", str(PORT)],
                            cwd=ROOT, env=_test_env(home_dir, dsn),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    state_path = os.path.join(home_dir, ".memnos", "gateway_state.json")
    deadline = time.monotonic() + 300      # cold start: model download possible
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


def gw_post(path, token, body=None, timeout=10):
    import httpx
    return httpx.post(URL + path, headers={"Authorization": f"Bearer {token}"},
                      json=body or {}, timeout=timeout)


class ContinuousTrafficHammer:
    """Fires GET /healthz through the PUBLIC port on brand-new TCP connections
    (http.client, keep-alive OFF) from several threads, continuously, until stopped.
    Tracks successes, non-200 failures, and connection REFUSALS separately — per issue
    #37 Layer 2's acceptance bar, both must be exactly zero across a real upgrade."""

    def __init__(self, host, port, workers=8):
        self.host, self.port, self.workers = host, port, workers
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.ok = 0
        self.fail = 0
        self.refused = 0
        self.errors = []
        self._threads = []

    def _worker(self):
        while not self._stop.is_set():
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
                conn.request("GET", "/healthz")
                resp = conn.getresponse()
                resp.read()
                status = resp.status
                conn.close()
                with self._lock:
                    if status == 200:
                        self.ok += 1
                    else:
                        self.fail += 1
                        if len(self.errors) < 20:
                            self.errors.append(f"HTTP {status}")
            except ConnectionRefusedError as e:
                with self._lock:
                    self.refused += 1
                    if len(self.errors) < 20:
                        self.errors.append(f"refused: {e}")
            except Exception as e:
                with self._lock:
                    self.fail += 1
                    if len(self.errors) < 20:
                        self.errors.append(f"{type(e).__name__}: {e}")

    def start(self):
        self._threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)


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
    pid_ = Control.create_principal(conn, "zdt-upgrade-agent", "agent")
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "zdt-upgrade-test")

    home_dir = tempfile.mkdtemp(prefix="memnos_zdt_home_")
    gw_proc = None
    try:
        print(f"=== starting gateway (issue #37 Layer 2) on {URL} ===")
        gw_proc, state = start_gateway(home_dir, dsn)
        ctl_token = state["control_token"]
        print(f"  gateway up, pid={gw_proc.pid}, control token acquired")

        st0 = gw_get("/__gateway__/status", ctl_token).json()
        old_pid = st0.get("current_backend_pid")
        old_port = st0.get("current_backend_port")
        check("gateway reports a live backend before the upgrade",
              bool(old_pid) and bool(old_port), st0)

        # write something through the OLD backend, before the swap, to verify continuity
        r = gw_post("/remember", token, {"namespace": NS, "text": "ZDT-UPGRADE marker: the "
                    "kingfisher relocated its nest to the old oak by the creek."})
        check("remember() before the upgrade succeeds", r.status_code == 200, r.text)

        hammer = ContinuousTrafficHammer("127.0.0.1", PORT, workers=8)
        hammer.start()
        time.sleep(1.0)                             # establish a baseline of real traffic

        print("=== triggering a real rolling (blue-green) upgrade ===")
        t_upgrade_start = time.monotonic()
        r = gw_post("/__gateway__/upgrade", ctl_token)
        check("upgrade request accepted (202)", r.status_code == 202, r.text)

        up = {}
        deadline = time.monotonic() + 300
        last_phase = None
        while time.monotonic() < deadline:
            st = gw_get("/__gateway__/status", ctl_token).json()
            up = st.get("upgrade") or {}
            if up.get("phase") != last_phase:
                print(f"  · phase: {up.get('phase')}")
                last_phase = up.get("phase")
            if up.get("status") in ("done", "failed"):
                break
            time.sleep(0.1)
        t_upgrade_end = time.monotonic()

        time.sleep(1.0)                              # keep hammering a bit past completion
        hammer.stop()

        check("upgrade completed successfully", up.get("status") == "done", up)
        st1 = gw_get("/__gateway__/status", ctl_token).json()
        new_pid = st1.get("current_backend_pid")
        new_port = st1.get("current_backend_port")
        check("a REAL backend swap happened (pid actually changed)",
              bool(new_pid) and new_pid != old_pid, f"old={old_pid} new={new_pid}")
        check("internal backend port also changed", new_port != old_port,
              f"old={old_port} new={new_port}")
        check("old backend reported drained", bool(up.get("drained")), up)

        print(f"\n  downtime window measured: upgrade took "
              f"{t_upgrade_end - t_upgrade_start:.2f}s wall-clock (gateway public port "
              "never stopped accepting connections during this window — see counts below)")
        print(f"  traffic during the ENTIRE test: ok={hammer.ok}  fail={hammer.fail}  "
              f"refused={hammer.refused}")
        check("zero failed requests against the public port during the whole window",
              hammer.fail == 0, f"fail={hammer.fail} sample_errors={hammer.errors[:5]}")
        check("zero connection-refused against the public port during the whole window",
              hammer.refused == 0, f"refused={hammer.refused} sample_errors={hammer.errors[:5]}")
        check("a real amount of traffic was actually exercised during the window (>100 reqs)",
              hammer.ok > 100, f"ok={hammer.ok}")

        # prove the NEW backend is fully functional, not just alive: recall the fact
        # written against the OLD backend before the swap, through the SAME public port.
        r = gw_post("/recall", token, {"namespace": NS, "query": "kingfisher nest oak creek"})
        check("recall() after the swap succeeds", r.status_code == 200, r.text)
        ctx = (r.json() or {}).get("context", "") if r.status_code == 200 else ""
        check("recall() after the swap returns the content written before it",
              "kingfisher" in ctx.lower(), ctx[:200])

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
