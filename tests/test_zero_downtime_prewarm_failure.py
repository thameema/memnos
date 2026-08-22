"""issue #37 Layer 2 — TEST 2: the new-instance-fails-to-start/prewarm case.

Acceptance bar (from the issue): "if the new instance fails to start or fails prewarm,
the old instance must keep serving traffic uninterrupted, and the upgrade should report
a clear failure rather than leaving the system in a half-swapped state."

Forces the SECOND `memnos serve` invocation a gateway makes (i.e. the rolling upgrade's
NEW backend — the FIRST invocation, the gateway's own initial backend, must succeed
normally) to fail immediately and deterministically. This is done with a tiny `memnos`
shim script placed first on PATH for the gateway subprocess — NOT a test-only hook added
to memnos_gateway.py or memnos_server.py itself, so production code carries zero
test-only branches for this. The shim passes every invocation straight through to this
checkout's real `memnos_cli.py` until a marker file is created (armed by this test right
before triggering the upgrade), at which point the NEXT `serve` invocation exits non-zero
immediately — no DB, no network, no timing dependency.

While that failing upgrade attempt is in progress, a continuous-traffic hammer proves the
OLD backend never stops serving, and the gateway's own /__gateway__/status is checked to
prove: the upgrade is reported `failed` with a clear error, and current_backend_pid/port
are UNCHANGED (nothing was half-swapped).

Run: python tests/test_zero_downtime_prewarm_failure.py
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
TEST_DB = "memnos_test_zero_downtime_prewarm_fail"
PORT = int(os.environ.get("MEMNOS_ZDT_FAIL_TEST_PORT", "8971"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:zero-downtime-prewarm-fail"
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


def make_failing_memnos_shim(shim_dir, fail_marker_path):
    """A `memnos` on PATH that passes every invocation straight through to THIS
    checkout's real memnos_cli.py — UNTIL `fail_marker_path` exists, at which point the
    next (and only the next) `serve` invocation fails immediately with a distinctive
    exit code, no DB/network/timing involved. The marker is removed after triggering
    once, so only exactly one spawn is affected — a real gateway would otherwise keep
    retrying against a permanently-broken PATH forever, which isn't what this test is
    proving (it's proving ONE bad upgrade attempt, not a broken install)."""
    script = f"""#!/bin/bash
if [ "$1" = "serve" ] && [ -e "{fail_marker_path}" ]; then
    rm -f "{fail_marker_path}"
    echo "[test-shim] forcing this backend spawn to fail (issue #37 Layer 2 prewarm-failure test)" >&2
    exit 17
fi
exec "{PY}" "{CLI_PATH}" "$@"
"""
    path = os.path.join(shim_dir, "memnos")
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def _test_env(home_dir, dsn, shim_dir):
    env = dict(os.environ)
    env["HOME"] = home_dir
    env["MEMNOS_DSN"] = dsn
    env["MEMNOS_PORT"] = str(PORT)
    env.setdefault("MEMNOS_SECRET_KEY", "Y2lfdGVzdF9rZXlfMzJfYnl0ZXNfZXhhY3RseV9vayE=")
    env["OPENAI_API_KEY"] = ""
    # the shim dir goes FIRST — shutil.which("memnos") inside memnos_gateway.py must find
    # our shim, not any real installed `memnos` elsewhere on this machine's normal PATH.
    env["PATH"] = shim_dir + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def start_gateway(home_dir, dsn, shim_dir):
    proc = subprocess.Popen([PY, CLI_PATH, "gateway", "--port", str(PORT)],
                            cwd=ROOT, env=_test_env(home_dir, dsn, shim_dir),
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


def gw_post(path, token, body=None, timeout=10):
    import httpx
    return httpx.post(URL + path, headers={"Authorization": f"Bearer {token}"},
                      json=body or {}, timeout=timeout)


class ContinuousTrafficHammer:
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
            except ConnectionRefusedError:
                with self._lock:
                    self.refused += 1
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
    pid_ = Control.create_principal(conn, "zdt-prewarm-fail-agent", "agent")
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "zdt-prewarm-fail-test")

    home_dir = tempfile.mkdtemp(prefix="memnos_zdt_fail_home_")
    shim_dir = tempfile.mkdtemp(prefix="memnos_zdt_fail_shim_")
    fail_marker = os.path.join(shim_dir, "FAIL_NEXT_SERVE")
    gw_proc = None
    try:
        make_failing_memnos_shim(shim_dir, fail_marker)

        print(f"=== starting gateway on {URL} (shim on PATH, not yet armed) ===")
        gw_proc, state = start_gateway(home_dir, dsn, shim_dir)
        ctl_token = state["control_token"]
        print(f"  gateway up, pid={gw_proc.pid} — initial backend booted through the "
              "shim's pass-through path, so gateway boot itself proves the shim doesn't "
              "break the normal case")

        st0 = gw_get("/__gateway__/status", ctl_token).json()
        old_pid = st0.get("current_backend_pid")
        old_port = st0.get("current_backend_port")
        check("gateway reports a live backend before the failed upgrade attempt",
              bool(old_pid) and bool(old_port), st0)

        r = gw_post("/remember", token, {"namespace": NS, "text": "ZDT-PREWARM-FAIL marker: "
                    "the lighthouse keeper logged a storm warning at midnight."})
        check("remember() against the old backend succeeds", r.status_code == 200, r.text)

        hammer = ContinuousTrafficHammer("127.0.0.1", PORT, workers=8)
        hammer.start()
        time.sleep(1.0)

        # arm the failure — the NEXT `serve` invocation (this upgrade's new backend) will
        # exit immediately instead of starting.
        open(fail_marker, "w").close()

        print("=== triggering an upgrade that MUST fail (new backend forced to die on spawn) ===")
        r = gw_post("/__gateway__/upgrade", ctl_token)
        check("upgrade request accepted (202) — failure is discovered ASYNCHRONOUSLY, "
              "not at request time", r.status_code == 202, r.text)

        up = {}
        deadline = time.monotonic() + 60
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

        time.sleep(1.0)
        hammer.stop()

        check("the upgrade is reported as FAILED (not silently 'done', not stuck 'running')",
              up.get("status") == "failed", up)
        check("the failure has a clear, non-empty error message", bool(up.get("error")), up)
        # requirement #6: never leaves the system half-swapped.
        check("the failure was discovered fast (spawn-death short-circuit, not a full "
              "readiness-timeout wait)", up.get("phase") in ("spawn_failed", "prewarm_failed"), up)

        st1 = gw_get("/__gateway__/status", ctl_token).json()
        check("current backend pid is UNCHANGED after the failed upgrade (nothing swapped)",
              st1.get("current_backend_pid") == old_pid,
              f"before={old_pid} after={st1.get('current_backend_pid')}")
        check("current backend port is UNCHANGED after the failed upgrade",
              st1.get("current_backend_port") == old_port,
              f"before={old_port} after={st1.get('current_backend_port')}")

        print(f"\n  traffic during the failed-upgrade attempt: ok={hammer.ok}  "
              f"fail={hammer.fail}  refused={hammer.refused}")
        check("the OLD backend kept serving EVERY request throughout the failed attempt "
              "(zero failures)", hammer.fail == 0, f"fail={hammer.fail} errors={hammer.errors[:5]}")
        check("zero connection-refused throughout the failed attempt",
              hammer.refused == 0, f"refused={hammer.refused} errors={hammer.errors[:5]}")
        check("a real amount of traffic was actually exercised (>50 reqs)",
              hammer.ok > 50, f"ok={hammer.ok}")

        # the old backend must still be fully functional after the failed attempt too.
        r = gw_post("/recall", token, {"namespace": NS, "query": "lighthouse keeper storm warning"})
        check("recall() still works against the (untouched) old backend after the "
              "failed upgrade", r.status_code == 200 and
              "lighthouse" in ((r.json() or {}).get("context", "")).lower(), r.text)

    finally:
        if gw_proc is not None:
            stop_gateway(gw_proc)
        shutil.rmtree(home_dir, ignore_errors=True)
        shutil.rmtree(shim_dir, ignore_errors=True)
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
