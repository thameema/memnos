"""Gateway orphan-backend cleanup: a backend spawned by memnos_gateway.py's
_spawn_backend must not survive the GATEWAY itself dying uncleanly.

Root cause (field-confirmed): the gateway's own clean-shutdown path (SIGTERM/SIGINT)
kills its current backend before exiting, but an uncatchable death -- SIGKILL, an
OOM-kill from real memory pressure, a hard crash -- skips that path entirely. The
backend is then orphaned: no gateway is left to ever route a request to it again, yet
it keeps holding its full embedding/reranker model residency. Repeated crash/restart
cycles under memory pressure accumulate these as abandoned `memnos-server` processes --
the same pressure that caused the crash in the first place, a self-reinforcing leak.

Fix: memnos_server.py's `_gateway_parent_watcher` (started only when MEMNOS_GATEWAY_PID
is set -- i.e. only for a gateway-managed backend, never a standalone/legacy-mode
`memnos serve`) polls that pid's liveness and self-terminates the instant it's gone.

This test uses REAL subprocesses and a REAL SIGKILL (the exact uncatchable-death case
the fix exists for, not a mock) against a throwaway database and a fully sandboxed
HOME, so it never touches the real ~/.memnos config.

Run: MEMNOS_DSN=... python tests/test_gateway_orphan_backend.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_TEST_GATEWAY_PORT", "58901"))
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


def main():
    home = tempfile.mkdtemp(prefix="memnos_gw_orphan_test_")
    env = dict(os.environ)
    env["HOME"] = home
    env["MEMNOS_CI"] = "1"
    env["MEMNOS_GATEWAY_WATCHER_INTERVAL_S"] = "0.3"   # fast poll so the test doesn't wait 5s+
    # force _spawn_backend's shutil.which("memnos") to miss, so it falls back to invoking
    # THIS checkout's own memnos_cli.py via sys.executable -- a dev machine can have other
    # (possibly stale/broken) `memnos` installs earlier on PATH, which would otherwise spawn
    # unrelated code instead of the fix under test.
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"   # sys.executable/cli_path are absolute anyway

    cli = os.path.join(ROOT, "memnos_cli.py")
    gw_proc = None
    backend_pid = None
    try:
        # provision the control plane against the real (throwaway-pointed) DSN, in the
        # sandboxed HOME, without starting anything yet
        r = subprocess.run([sys.executable, cli, "setup", "--dsn", DSN],
                           env=env, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            sys.exit(f"setup failed:\n{r.stdout}\n{r.stderr}")

        print("=== gateway orphan-backend cleanup: real SIGKILL, no mocking ===")
        gw_proc = subprocess.Popen(
            [sys.executable, cli, "gateway", "--port", str(PORT)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        ready = _wait_http(f"http://127.0.0.1:{PORT}/healthz")
        check("gateway + backend became ready", ready)
        if not ready:
            return

        state_path = os.path.join(home, ".memnos", "gateway_state.json")
        with open(state_path) as f:
            state = json.load(f)
        token = state["control_token"]
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/__gateway__/status",
                                     headers={"Authorization": "Bearer " + token})
        status = json.load(urllib.request.urlopen(req, timeout=3))
        gateway_pid = status["gateway_pid"]
        backend_pid = status["current_backend_pid"]

        check("gateway_pid matches the spawned process", gateway_pid == gw_proc.pid,
              f"{gateway_pid} != {gw_proc.pid}")
        check("backend is a REAL, different, live process", backend_pid and backend_pid != gateway_pid
              and _pid_alive(backend_pid), f"backend_pid={backend_pid}")

        # the uncatchable-death case this fix exists for: SIGKILL the gateway directly,
        # bypassing its own clean-shutdown (SIGTERM/SIGINT) path entirely.
        os.kill(gateway_pid, signal.SIGKILL)
        gw_proc.wait(timeout=5)
        check("gateway process is actually gone", not _pid_alive(gateway_pid))

        # the backend should be alive for a moment (orphaned), then self-terminate once
        # its watcher thread's next poll notices the gateway is gone.
        backend_gone = False
        for _ in range(40):   # bounded well above the 0.3s poll interval
            if not _pid_alive(backend_pid):
                backend_gone = True
                break
            time.sleep(0.25)
        check("orphaned backend self-terminated instead of leaking", backend_gone,
              f"pid {backend_pid} still alive after SIGKILLing its gateway")
    finally:
        if gw_proc is not None and gw_proc.poll() is None:
            try:
                gw_proc.kill()
                gw_proc.wait(timeout=5)
            except Exception:
                pass
        # belt + suspenders: if the fix regressed and the backend didn't self-terminate,
        # don't leave it running after this test exits. gateway_state.json never carries
        # current_backend_pid (that's only in the live /__gateway__/status response, which
        # we already captured above while the gateway was still alive) -- use that, not a
        # re-read that would silently find nothing.
        try:
            if backend_pid and _pid_alive(backend_pid):
                os.kill(backend_pid, signal.SIGKILL)
        except Exception:
            pass
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
