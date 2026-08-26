"""adversarial-review finding — TEST: a NEW backend that dies AFTER passing readiness
(and after the atomic flip) but BEFORE the drain of the OLD backend completes must NOT
result in the OLD backend being killed, must NOT be reported as a successful upgrade, and
must leave the gateway in a live, non-corrupted state.

Confirmed blocking bug (adversarial review of memnos_gateway.py, shipped in memnos
0.1.32): `_wait_ready` proves the NEW backend's readiness exactly ONCE, right before the
atomic flip. From that instant, through the drain wait for the OLD backend (which can run
for up to MEMNOS_GATEWAY_DRAIN_TIMEOUT_S) and up to the `_kill(old_proc)` call, the new
backend's liveness was never re-checked. If it dies anywhere in that window — OOM under
first real traffic, a config issue that only trips on a real query, connection-pool
exhaustion — the UNPATCHED code:
  1. still kills the healthy OLD backend anyway (the drain-timeout logic doesn't care
     whether the NEW backend is still alive)
  2. leaves `_current_port` pointing at the now-dead NEW backend's port
  3. reports the upgrade as `status: done` — a false success

Because `launchd`/`systemd` supervise the GATEWAY process (not the backend) as of the
same release, this is a PERMANENT, silent outage, not a transient blip: the gateway
process itself stays up and "healthy" from the supervisor's point of view while nothing
behind it is actually serving traffic.

This test:
  1. proves the OLD (broken) behavior for real against an unpatched checkout, by killing
     a genuine, already-`/readyz`-proven NEW backend process at a realistic point — after
     the atomic flip, mid-drain — and observing the gateway's ACTUAL response (not a
     stubbed/mocked repro)
  2. against the FIXED checkout, proves: the OLD backend is never killed, the upgrade is
     reported FAILED with a clear error, `_current_port`/`_current_proc` roll back to the
     still-alive OLD backend, the dead NEW backend leaves no zombie/orphan, and a SECOND
     real upgrade afterward still completes cleanly — proving the rollback left no
     corrupted state behind

The drain window is held open using the SAME existing reranker test hooks
(MEMNOS_RERANK_SIMULATED_MS_PER_PAIR, issue #34) test_zero_downtime_inflight_drain.py
already uses — several slow recall() calls are kept CONTINUOUSLY in flight against the
OLD backend (a small pool of workers, each firing another slow recall the instant its
previous one completes) so `_wait_drain` cannot return "drained" before this test has had
a chance to observe the "draining" phase (proof the flip already happened) and kill the
NEW backend's real OS pid — regardless of how long the new backend's own prewarm happens
to take on a given machine.

Run: python tests/test_zero_downtime_new_backend_death.py
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
TEST_DB = "memnos_test_zero_downtime_new_backend_death"
PORT = int(os.environ.get("MEMNOS_ZDT_DEATH_TEST_PORT", "8973"))
URL = f"http://127.0.0.1:{PORT}"
NS = "test:zero-downtime-new-backend-death"
N_SEED_FACTS = 20
N_BUSY_WORKERS = 3
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
    """Isolated HOME (no real ~/.memnos/config.json leak) — same convention every other
    zero-downtime test in this file uses."""
    env = dict(os.environ)
    env["HOME"] = home_dir
    env["MEMNOS_DSN"] = dsn
    env["MEMNOS_PORT"] = str(PORT)
    env.setdefault("MEMNOS_SECRET_KEY", "Y2lfdGVzdF9rZXlfMzJfYnl0ZXNfZXhhY3RseV9vayE=")
    env["OPENAI_API_KEY"] = ""
    # same existing rerank test hooks test_zero_downtime_inflight_drain.py uses — NOT new
    # for this test — to make recall() deterministically slow so several of them can be
    # kept genuinely in-flight against the OLD backend for as long as this test needs.
    env["MEMNOS_RERANK_SIMULATED_MS_PER_PAIR"] = "400"
    env["MEMNOS_RERANK_MIN_CAP"] = "15"
    env["MEMNOS_FAKE_EXTRACT"] = "1"
    # generous drain budget — this test deliberately holds the drain window open with a
    # continuous stream of slow recalls; a tight timeout would let the drain wait
    # short-circuit (and kill old_proc on a bare timeout) before this test gets to
    # exercise the actual liveness recheck the fix adds.
    env["MEMNOS_GATEWAY_DRAIN_TIMEOUT_S"] = "60"
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
    """Block until the backend's own reranker background calibration has finished — a
    recall() issued before that silently skips reranking, which would break this test's
    timing assumptions."""
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
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_zombie(pid):
    """True if `pid` currently exists as a zombie (defunct, unreaped) process. An empty
    `ps` result (pid fully gone — the common, correct outcome once the gateway has
    reaped it) is NOT a zombie."""
    if not pid:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return "Z" in (out.stdout or "")
    except Exception:
        return False


def gateway_children(gateway_pid):
    """Direct child pids of the gateway process per the OS — the ground truth for "did
    this leave anything orphaned/running that shouldn't be". None means pgrep isn't
    available on this box (soft-skip, not a failure)."""
    try:
        out = subprocess.run(["pgrep", "-P", str(gateway_pid)],
                             capture_output=True, text=True, timeout=5)
        if out.returncode not in (0, 1):     # 1 = "no processes matched", still valid
            return None
        return sorted(int(p) for p in out.stdout.split() if p.strip())
    except Exception:
        return None


class ContinuousTrafficHammer:
    """Fires GET /healthz through the PUBLIC port on brand-new TCP connections,
    continuously, from several threads. Proves the public port itself never refuses a
    connection during the incident — the gateway's core zero-downtime promise — even
    while individual proxied requests may transiently 502 in the split second the new
    backend is actually dead and the rollback hasn't landed yet."""

    def __init__(self, host, port, workers=6):
        self.host, self.port, self.workers = host, port, workers
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.ok = 0
        self.fail = 0
        self.refused = 0
        self.errors = []
        self._threads = []

    def _worker(self):
        import http.client
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
    pid_ = Control.create_principal(conn, "zdt-death-agent", "agent")
    Control.grant(conn, pid_, NS)
    token = Control.mint_token(conn, pid_, "zdt-death-test")

    home_dir = tempfile.mkdtemp(prefix="memnos_zdt_death_home_")
    gw_proc = None
    try:
        print(f"=== starting gateway on {URL} (reranker simulated 400ms/pair, drain timeout 60s) ===")
        gw_proc, state = start_gateway(home_dir, dsn)
        ctl_token = state["control_token"]
        gateway_pid = gw_proc.pid
        print(f"  gateway up, pid={gateway_pid}")

        st0 = gw_get("/__gateway__/status", ctl_token).json()
        old_pid = st0.get("current_backend_pid")
        old_port = st0.get("current_backend_port")
        check("gateway reports a live old backend before the test begins",
              bool(old_pid) and bool(old_port), st0)

        rr_ready = wait_rerank_ready(token)
        check("the backend's own reranker calibration finished before the test proceeds",
              rr_ready)

        r = gw_post("/remember", token, {"namespace": NS, "text": "ZDT-DEATH marker: the "
                    "harbor pilot logged a fogbound approach off Seal Point at dawn."})
        check("remember() before the incident succeeds", r.status_code == 200, r.text)

        print(f"=== seeding {N_SEED_FACTS} facts for a non-trivial rerank candidate pool ===")
        for i in range(N_SEED_FACTS):
            r = gw_post("/remember", token, {"namespace": NS, "text":
                        f"Buoy Meridian{i} of the Coastal Current Study logged a Drift "
                        f"Speed of {50 + i} centimeters/second near Tern Shoal."})
            if r.status_code != 200:
                raise RuntimeError(f"seed remember #{i} failed: {r.status_code} {r.text}")

        # ---- keep the OLD backend continuously busy with slow recalls, for as long as
        # needed — a pool of workers, each firing another slow recall the instant its
        # previous one completes, so in-flight[old_port] stays > 0 no matter how long the
        # new backend's own prewarm takes on this machine.
        recall_results = []
        recall_lock = threading.Lock()
        stop_recalls = threading.Event()

        def keep_old_backend_busy(worker_id):
            n = 0
            while not stop_recalls.is_set():
                t0 = time.monotonic()
                res = {"worker": worker_id, "n": n, "t_start": t0}
                try:
                    r = gw_post("/recall", token,
                               {"namespace": NS, "query": "Coastal Current Study drift speed Meridian buoy"},
                               timeout=120)
                    res["status"] = r.status_code
                    res["body"] = r.json() if r.status_code == 200 else r.text
                except Exception as e:
                    res["exception"] = f"{type(e).__name__}: {e}"
                res["t_end"] = time.monotonic()
                with recall_lock:
                    recall_results.append(res)
                n += 1

        hammer = ContinuousTrafficHammer("127.0.0.1", PORT, workers=6)
        hammer.start()

        print(f"=== starting {N_BUSY_WORKERS} continuous slow-recall workers against the "
              "OLD backend (holds the drain window open indefinitely) ===")
        busy_threads = [threading.Thread(target=keep_old_backend_busy, args=(w,), daemon=True)
                        for w in range(N_BUSY_WORKERS)]
        for th in busy_threads:
            th.start()
        time.sleep(1.5)      # let them actually dispatch and land on the old backend

        st_inflight = gw_get("/__gateway__/status", ctl_token).json()
        in_flight_n = (st_inflight.get("inflight") or {}).get(str(old_port), 0)
        check("gateway's own in-flight counter shows the busy workers in-flight on the "
              "OLD backend before the upgrade is triggered", in_flight_n >= 1,
              st_inflight.get("inflight"))

        print("=== triggering the rolling upgrade, then killing the NEW backend the "
              "instant it has passed readiness and the flip has happened (mid-drain) ===")
        r = gw_post("/__gateway__/upgrade", ctl_token)
        check("upgrade request accepted (202)", r.status_code == 202, r.text)

        new_pid = None
        killed_at = None
        up = {}
        deadline = time.monotonic() + 300
        last_phase = None
        while time.monotonic() < deadline:
            st = gw_get("/__gateway__/status", ctl_token).json()
            up = st.get("upgrade") or {}
            phase = up.get("phase")
            if phase != last_phase:
                print(f"  · phase: {phase}")
                last_phase = phase
            if up.get("new_pid") and new_pid is None:
                new_pid = up["new_pid"]
            if phase == "draining" and killed_at is None:
                # readiness already proven + the atomic flip already happened (this
                # phase is only ever set right after both) — this IS the post-readiness,
                # pre-drain-complete window the confirmed bug lives in.
                if new_pid:
                    os.kill(new_pid, signal.SIGKILL)
                    killed_at = time.monotonic()
                    print(f"  · killed new backend pid={new_pid} (SIGKILL) — simulating a "
                          "post-readiness crash (OOM/config/pool-exhaustion class failure)")
                else:
                    print("  · WARNING: reached draining phase without a known new_pid")
            if up.get("status") in ("done", "failed"):
                break
            time.sleep(0.1)
        concluded_at = time.monotonic()

        check("the kill actually happened during the drain window (not skipped because "
              "the phase transition was missed)", killed_at is not None, up)

        stop_recalls.set()
        for th in busy_threads:
            th.join(timeout=15)
        time.sleep(1.0)
        hammer.stop()

        # ---- the core red/green assertions ---------------------------------------
        check("the upgrade is reported FAILED, not silently 'done' — a new backend that "
              "died post-readiness must never be reported as a successful upgrade",
              up.get("status") == "failed", up)
        check("the failure has a clear, non-empty error message", bool(up.get("error")), up)
        check("the failure error mentions the dead new backend's pid",
              str(new_pid) in str(up.get("error", "")), up.get("error"))

        st1 = gw_get("/__gateway__/status", ctl_token).json()
        check("current_backend_pid rolled back to the OLD (still-alive) backend, not "
              "left pointing at the dead new one", st1.get("current_backend_pid") == old_pid,
              f"old={old_pid} current={st1.get('current_backend_pid')}")
        check("current_backend_port rolled back to the OLD backend's port",
              st1.get("current_backend_port") == old_port,
              f"old={old_port} current={st1.get('current_backend_port')}")

        check("the OLD backend process is still alive (never killed)", pid_alive(old_pid),
              f"old_pid={old_pid}")
        check("the dead NEW backend process was fully reaped — no zombie left behind",
              not pid_zombie(new_pid), f"new_pid={new_pid}")

        children = gateway_children(gateway_pid)
        check("the gateway has exactly one remaining child process (the still-alive OLD "
              "backend) — nothing orphaned", children is None or children == [old_pid],
              f"children={children} expected=[{old_pid}]")

        # ---- did the continuously in-flight recalls against the OLD backend survive? --
        pre_crash = [r for r in recall_results if r["t_start"] < killed_at]
        post_recovery = [r for r in recall_results if r["t_start"] > concluded_at]
        ambiguous = [r for r in recall_results if killed_at <= r["t_start"] <= concluded_at]

        def ok(r):
            return "exception" not in r and r.get("status") == 200

        check("at least one recall() was genuinely in-flight against the OLD backend at "
              "the moment of the crash (the scenario this test claims to exercise)",
              len(pre_crash) >= 1, f"pre_crash={len(pre_crash)}")
        check("every recall() that started BEFORE the crash (genuinely in-flight against "
              "the OLD backend at that moment) completed successfully, uninterrupted",
              all(ok(r) for r in pre_crash),
              [{"worker": r["worker"], "status": r.get("status"), "exception": r.get("exception")}
               for r in pre_crash if not ok(r)])
        check("every recall() that started AFTER the gateway reported the upgrade's "
              "final status succeeded (traffic is fully recovered post-incident)",
              len(post_recovery) == 0 or all(ok(r) for r in post_recovery),
              [{"worker": r["worker"], "status": r.get("status"), "exception": r.get("exception")}
               for r in post_recovery if not ok(r)])
        good_contents = [((r.get("body") or {}).get("context", "")) for r in pre_crash + post_recovery
                         if ok(r) and isinstance(r.get("body"), dict)]
        check("the successful recalls returned the correct seeded content (not dropped, "
              "not a blank/degraded response)",
              bool(good_contents) and all("meridian" in c.lower() for c in good_contents),
              [c[:150] for c in good_contents if "meridian" not in c.lower()])
        if ambiguous:
            amb_fail = [r for r in ambiguous if not ok(r)]
            print(f"  · info: {len(ambiguous)} recall(s) started in the ambiguous window "
                  f"between the crash and the gateway settling; {len(amb_fail)} of them "
                  "failed — expected/harmless (any real crash has a brief blast radius "
                  "for whatever was dispatched in that instant); not asserted on.")

        # ---- traffic through the PUBLIC port must keep working throughout -------------
        print(f"\n  /healthz hammer during the whole incident: ok={hammer.ok}  "
              f"fail={hammer.fail}  refused={hammer.refused}")
        check("the public port never REFUSED a connection during the whole incident "
              "(the gateway's own front-door promise)", hammer.refused == 0,
              f"refused={hammer.refused} errors={hammer.errors[:5]}")
        # A real crash always has a brief blast radius: whatever the hammer had already
        # dispatched into the dead new backend right as it died, plus whatever lands in
        # the crash-to-rollback detection window, will genuinely 502 — that is not a bug.
        # A SUSTAINED outage would instead show as a large fraction of the whole
        # multi-second run failing, not a short cluster; a proportional bound (not a
        # fixed count) is what actually distinguishes the two regardless of hammer
        # concurrency/speed/hardware.
        hammer_total = hammer.ok + hammer.fail
        hammer_fail_rate = (hammer.fail / hammer_total) if hammer_total else 0.0
        check("the public port did not degenerate into a sustained outage (a brief "
              "cluster of 502s right at the crash is expected; a large sustained "
              "failure rate across the whole run is not)",
              hammer_fail_rate < 0.10,
              f"fail={hammer.fail} ok={hammer.ok} rate={hammer_fail_rate:.1%} "
              f"errors={hammer.errors[:5]}")

        r = gw_post("/remember", token, {"namespace": NS, "text": "ZDT-DEATH post-incident "
                    "marker: the tide gauge recalibrated after the storm passed."})
        check("a FRESH remember() through the public port succeeds AFTER the incident "
              "(the gateway is actually still serving traffic, not just claiming to be)",
              r.status_code == 200, r.text)
        r = gw_post("/recall", token, {"namespace": NS, "query": "harbor pilot fogbound approach Seal Point"})
        check("a FRESH recall() through the public port, for content written BEFORE the "
              "incident, still succeeds and is correct AFTER the incident",
              r.status_code == 200 and "fogbound" in ((r.json() or {}).get("context", "")).lower(),
              r.text)

        # ---- prove the rollback left no corrupted state: a SECOND real upgrade must ----
        # still work cleanly from here.
        print("=== triggering a SECOND real upgrade to prove the rollback left no "
              "corrupted _current_port/_current_proc state behind ===")
        r = gw_post("/__gateway__/upgrade", ctl_token)
        check("second upgrade request accepted (202)", r.status_code == 202, r.text)
        up2 = {}
        deadline2 = time.monotonic() + 300
        while time.monotonic() < deadline2:
            st = gw_get("/__gateway__/status", ctl_token).json()
            up2 = st.get("upgrade") or {}
            if up2.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        check("the second upgrade completed successfully", up2.get("status") == "done", up2)
        st2 = gw_get("/__gateway__/status", ctl_token).json()
        check("the second upgrade produced a REAL new pid, different from the "
              "rolled-back-to old one — proves _current_port/_current_proc were not left "
              "corrupted by the rollback", bool(st2.get("current_backend_pid")) and
              st2.get("current_backend_pid") != old_pid,
              f"old={old_pid} after_second_upgrade={st2.get('current_backend_pid')}")

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
