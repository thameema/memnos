"""memnos gateway backend watchdog: a backend that dies OUTSIDE any rolling upgrade
(crash, OOM-kill, a wedge) must be auto-healed by the gateway instead of leaving it
squatting the public port forever, 502ing every request.

Root cause (field-confirmed, real incident): the v0.1.39 gateway/front-door architecture
(#37 Layer 2) had NO monitoring of its own backend during normal (non-upgrade) operation
-- only `_run_rolling_upgrade` ever replaces `_current_proc`, and that only runs when a
human/CLI explicitly asks for one. A backend that died on its own (OOM-killed under real
memory pressure) left the gateway alive, holding :8900, returning 502 forever. Confirmed
on a real box: a `memnos-gateway` process 15 days old, its backend long dead, found only
when a human happened to investigate -- `memnos status` said "not running" (health check
502s through the dead-backend proxy) while `memnos start` hung >120s on the still-occupied
port (its own retry loop polling a zombie that could never self-fix).

Fix: `_watchdog_loop` (memnos_gateway.py) runs for the gateway's whole life, polls backend
liveness (process exit -- immediate; or WATCHDOG_MISS_THRESHOLD consecutive /healthz
misses -- debounced), and on a confirmed-dead backend spawns + swaps in a replacement,
reusing the same `_spawn_backend` + `_wait_ready` machinery `_run_rolling_upgrade` already
uses for planned upgrades. After MEMNOS_GATEWAY_WATCHDOG_MAX_HEAL_ATTEMPTS consecutive
failed heals, the gateway gives up and exits non-zero -- frees the port and lets `memnos
status` tell the truth, instead of staying alive unable to ever serve again (leaving the
underlying box's resource problem, if that's the actual cause, to autostart's own
restart-on-failure / a human, same as any other crash).

Covers:
  1. (real subprocess, real SIGKILL -- no mocking) Kill the BACKEND with a healthy DSN
     (the inverse of test_gateway_orphan_backend.py, which kills the GATEWAY): the
     gateway self-heals within one watchdog cycle -- a NEW, different backend pid comes
     up, public /healthz through the gateway returns 200 again, and
     /__gateway__/status's watchdog.heal_count reflects the heal. Also checks the
     /healthz clarity fix: a confirmed-dead backend answers 503 + a "watchdog" field,
     not a bare, ambiguous 502.
  2. (real subprocess, real SIGSTOP -- no mocking) The OTHER death signal: freeze the
     backend instead of killing it (`poll()` stays None -- it's still a live process,
     just unresponsive -- the actual shape a severely swap-thrashed box produces, the
     originating incident's root cause). Exercises the debounced /healthz-miss detection
     path specifically, not the immediate process-exit path case 1 already covers.
  3. (in-process unit test of `_watchdog_loop` itself) A backend that can NEVER be
     healed: `_spawn_backend`/`_wait_ready` are monkeypatched so every replacement
     reports "never became ready" -- deterministic, and avoids the one real way to force
     that end-to-end (an unreachable Postgres) being unusable here: the DSN a spawned
     backend uses is cached once in the GATEWAY PROCESS's own environment at boot
     (`_apply_env`'s `os.environ.setdefault`) and inherited by every child it spawns
     via `env = dict(os.environ)` -- corrupting config.json on disk AFTER the gateway
     has booted does not reach already-running or future-spawned children at all, so a
     real end-to-end version of this specific case cannot be driven through a live
     subprocess without changing unrelated code. The watchdog's own give-up/exit DECISION
     logic -- the actual new code this test exists to cover -- is exercised for real.
     (This mirrors tests/test_setup_embedded_default.py's own unsupported-platform check,
     which is in-process for the same class of reason.)

Run: MEMNOS_DSN=... python tests/test_gateway_backend_autoheal.py
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
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT_A = int(os.environ.get("MEMNOS_TEST_GATEWAY_AUTOHEAL_PORT", "58911"))
PORT_WEDGED = int(os.environ.get("MEMNOS_TEST_GATEWAY_AUTOHEAL_WEDGED_PORT", "58913"))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _wait_http(url, tries=60, delay=0.5):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(delay)
    return False


def _gateway_status(port, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/__gateway__/status",
                                 headers={"Authorization": "Bearer " + token})
    return json.load(urllib.request.urlopen(req, timeout=3))


def test_self_heals_a_dead_backend(cli, env, home):
    print("=== real subprocess, real SIGKILL: dead backend (good DSN) self-heals ===")
    env = dict(env)
    env["MEMNOS_GATEWAY_WATCHDOG_INTERVAL_S"] = "0.5"
    env["MEMNOS_GATEWAY_WATCHDOG_MISS_THRESHOLD"] = "1"   # the proc-exit path skips the
                                                            # debounce anyway (SIGKILL is
                                                            # unambiguous) -- small either way
    gw_proc = subprocess.Popen([sys.executable, cli, "gateway", "--port", str(PORT_A)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ready = _wait_http(f"http://127.0.0.1:{PORT_A}/healthz")
        check("gateway + initial backend became ready", ready)
        if not ready:
            return

        state_path = os.path.join(home, ".memnos", "gateway_state.json")
        with open(state_path) as f:
            state = json.load(f)
        token = state["control_token"]
        status = _gateway_status(PORT_A, token)
        old_backend_pid = status["current_backend_pid"]
        check("have a real backend pid before killing it", bool(old_backend_pid))

        os.kill(old_backend_pid, signal.SIGKILL)

        # the /healthz clarity fix: a confirmed-dead backend should answer with a clear
        # 503 + a "watchdog" field, not a bare, ambiguous 502 — catch it in the narrow
        # window before the watchdog's own heal completes (retried briefly: how fast the
        # OS actually tears down the dead backend's socket is not instant).
        saw_watchdog_503 = False
        for _ in range(20):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT_A}/healthz", timeout=1)
            except urllib.error.HTTPError as e:
                if e.code == 503:
                    body = json.loads(e.read())
                    saw_watchdog_503 = body.get("ok") is False and "watchdog" in body
                    break
            except Exception:
                pass
            time.sleep(0.1)
        check("public /healthz on a confirmed-dead backend is a clear 503 + watchdog state "
              "(not a bare 502)", saw_watchdog_503)

        healed = False
        new_backend_pid = None
        last_status = None
        for _ in range(240):      # bounded well above detect (~0.5s) + a real backend boot
            try:
                last_status = _gateway_status(PORT_A, token)
            except Exception:
                time.sleep(0.5)
                continue
            pid = last_status.get("current_backend_pid")
            if pid and pid != old_backend_pid and _pid_alive(pid):
                new_backend_pid = pid
                healed = True
                break
            time.sleep(0.5)
        check("a NEW, different backend pid came up after the kill", healed,
              f"old={old_backend_pid} last_status={last_status}")
        if not healed:
            return

        up = _wait_http(f"http://127.0.0.1:{PORT_A}/healthz")
        check("public /healthz is healthy again through the gateway", up)

        final_status = _gateway_status(PORT_A, token)
        check("watchdog.heal_count is exactly 1 (one kill, one heal — not a double-fire)",
              (final_status.get("watchdog") or {}).get("heal_count") == 1,
              final_status.get("watchdog"))
        check("old (killed) backend pid is actually gone", not _pid_alive(old_backend_pid))
    finally:
        if gw_proc.poll() is None:
            try:
                gw_proc.kill()
                gw_proc.wait(timeout=5)
            except Exception:
                pass


def test_self_heals_a_wedged_backend(cli, env, home):
    """The OTHER death signal: a backend that is still a live process (`proc.poll()` stays
    None — SIGSTOP freezes it, it does not exit) but never answers a socket. This is the
    actual failure shape a severely swap-thrashed box produces (the originating incident's
    root cause), as opposed to a clean process exit — and exercises the DEBOUNCED
    /healthz-miss path (`WATCHDOG_MISS_THRESHOLD` consecutive failures), not the immediate
    proc-exit path `test_self_heals_a_dead_backend` already covers."""
    print("=== real subprocess, real SIGSTOP: wedged (alive but unresponsive) backend self-heals ===")
    env = dict(env)
    env["MEMNOS_GATEWAY_WATCHDOG_INTERVAL_S"] = "0.5"
    env["MEMNOS_GATEWAY_WATCHDOG_HEALTHCHECK_TIMEOUT_S"] = "0.5"
    env["MEMNOS_GATEWAY_WATCHDOG_MISS_THRESHOLD"] = "2"
    env["MEMNOS_GATEWAY_KILL_GRACE_S"] = "1"   # bound how long _kill takes to reap the
                                                 # frozen old backend (SIGTERM's default
                                                 # disposition terminates a stopped process
                                                 # outright on this platform — verified —
                                                 # but keep the fallback SIGKILL grace short
                                                 # regardless, so this test stays fast even
                                                 # somewhere that differs)
    gw_proc = subprocess.Popen([sys.executable, cli, "gateway", "--port", str(PORT_WEDGED)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    old_backend_pid = None
    try:
        ready = _wait_http(f"http://127.0.0.1:{PORT_WEDGED}/healthz")
        check("gateway + initial backend became ready", ready)
        if not ready:
            return

        state_path = os.path.join(home, ".memnos", "gateway_state.json")
        with open(state_path) as f:
            state = json.load(f)
        token = state["control_token"]
        status = _gateway_status(PORT_WEDGED, token)
        old_backend_pid = status["current_backend_pid"]
        check("have a real backend pid before freezing it", bool(old_backend_pid))

        os.kill(old_backend_pid, signal.SIGSTOP)   # alive (poll() stays None), unresponsive

        healed = False
        new_backend_pid = None
        last_status = None
        for _ in range(240):
            try:
                last_status = _gateway_status(PORT_WEDGED, token)
            except Exception:
                time.sleep(0.5)
                continue
            pid = last_status.get("current_backend_pid")
            if pid and pid != old_backend_pid and _pid_alive(pid):
                new_backend_pid = pid
                healed = True
                break
            time.sleep(0.5)
        check("a NEW, different backend pid came up after the wedge (detected via "
              "/healthz misses, not a process exit)", healed,
              f"old={old_backend_pid} last_status={last_status}")
        if not healed:
            return

        up = _wait_http(f"http://127.0.0.1:{PORT_WEDGED}/healthz")
        check("public /healthz is healthy again through the gateway", up)

        final_status = _gateway_status(PORT_WEDGED, token)
        check("watchdog.heal_count is exactly 1",
              (final_status.get("watchdog") or {}).get("heal_count") == 1,
              final_status.get("watchdog"))
    finally:
        try:
            if old_backend_pid and _pid_alive(old_backend_pid):
                os.kill(old_backend_pid, signal.SIGCONT)   # belt + suspenders: in case the
                os.kill(old_backend_pid, signal.SIGKILL)   # watchdog's own cleanup didn't
        except Exception:
            pass
        if gw_proc.poll() is None:
            try:
                gw_proc.kill()
                gw_proc.wait(timeout=5)
            except Exception:
                pass


def test_gives_up_after_max_heal_attempts_unit():
    print("=== unit: _watchdog_loop gives up + exits after MAX_HEAL_ATTEMPTS failures ===")
    import memnos_gateway as gw

    # isolate from anything a prior test in this process might have left behind
    gw._current_port = None
    gw._current_proc = None
    gw._watchdog_gave_up = False
    with gw._heal_lock:
        gw._heal_stats.update(heal_count=0, consecutive_heal_failures=0, last_heal_at=None)

    gw.WATCHDOG_INTERVAL_S = 0.05
    gw.WATCHDOG_MISS_THRESHOLD = 1
    gw.WATCHDOG_HEAL_BACKOFF_S = 0.05
    gw.WATCHDOG_MAX_HEAL_ATTEMPTS = 2

    # The "backend": a real, killable process -- only whether a REPLACEMENT becomes ready
    # is faked (see module docstring for why driving this end-to-end isn't practical here).
    dead_backend = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead_backend.kill()
    dead_backend.wait(timeout=5)
    gw._current_port, gw._current_proc = 1, dead_backend

    spawned = []

    def fake_spawn_backend(port):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        spawned.append(p)
        return p

    def fake_wait_ready(port, timeout, proc=None):
        return False   # every replacement "never becomes ready" -- deterministic

    orig_spawn, orig_wait = gw._spawn_backend, gw._wait_ready
    gw._spawn_backend = fake_spawn_backend
    gw._wait_ready = fake_wait_ready
    try:
        stop_evt = threading.Event()
        t = threading.Thread(target=gw._watchdog_loop, args=(stop_evt,), daemon=True)
        t.start()
        gave_up = stop_evt.wait(timeout=10)
        t.join(timeout=2)

        check("watchdog set stop_evt after exhausting heal attempts", gave_up)
        check("_watchdog_gave_up flag is set", gw._watchdog_gave_up is True)
        check(f"consecutive_heal_failures reached MAX_HEAL_ATTEMPTS ({gw.WATCHDOG_MAX_HEAL_ATTEMPTS})",
              gw._heal_stats["consecutive_heal_failures"] == gw.WATCHDOG_MAX_HEAL_ATTEMPTS,
              gw._heal_stats)
        check(f"made exactly {gw.WATCHDOG_MAX_HEAL_ATTEMPTS} failed spawn attempts",
              len(spawned) == gw.WATCHDOG_MAX_HEAL_ATTEMPTS, len(spawned))
        check("heal_count stayed 0 -- none of the failed attempts counted as a success",
              gw._heal_stats["heal_count"] == 0, gw._heal_stats)
    finally:
        gw._spawn_backend = orig_spawn
        gw._wait_ready = orig_wait
        for p in spawned + [dead_backend]:
            if p.poll() is None:
                p.kill()


def main():
    home = tempfile.mkdtemp(prefix="memnos_gw_autoheal_test_")
    env = dict(os.environ)
    env["HOME"] = home
    env["MEMNOS_CI"] = "1"
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"   # see test_gateway_orphan_backend.py: force
                                                      # the fallback to THIS checkout's CLI,
                                                      # not some other `memnos` earlier on PATH

    cli = os.path.join(ROOT, "memnos_cli.py")
    try:
        r = subprocess.run([sys.executable, cli, "setup", "--dsn", DSN],
                           env=env, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            sys.exit(f"setup failed:\n{r.stdout}\n{r.stderr}")

        test_self_heals_a_dead_backend(cli, env, home)
        test_self_heals_a_wedged_backend(cli, env, home)
        test_gives_up_after_max_heal_attempts_unit()
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
