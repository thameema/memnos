"""
Real runtime behavioral test for the #77 SIGINT/cleanup fix in
tommy.cli._launch_harness.

TestCLILaunchHarness in test_control.py only regex-scans cli.py's source
text (no start_new_session=True string, finally: appears before ctrl.close()
and os.unlink). It never spawns a process or sends a signal, so it cannot
tell a correct fix from a plausible-looking one that regresses at runtime.

This module spawns a real "tommy" stand-in process (fixtures/sigint_driver.py)
that calls the REAL, unmodified _launch_harness() end-to-end — real Popen,
real ControlServer, real tempfile prompt — against a stub "harness" process
(fixtures/sigint_harness_stub.py). The driver is started with
start_new_session=True so it becomes its own process-group leader; the
harness it launches (without start_new_session, per the #77 fix) inherits
that group. Sending SIGINT to the group with os.killpg() is mechanically
identical to a terminal Ctrl-C reaching the foreground process group — no
pty or controlling terminal required, so this runs clean under a headless
CI runner.

Scope note: this test targets the process-group/signal-delivery mechanism
specifically (does the harness receive and act on SIGINT; is it actually
dead; are prompt file + control socket really cleaned up). It does not
reproduce the historical "tommy exits while the harness keeps running"
symptom verbatim, because the try/finally introduced by #77 (not toggled by
this test) makes the driver block in proc.wait() for the harness either way.
The red/green proof below (see PR description) toggles only the
start_new_session=True line and shows the harness then fails to receive/act
on the signal within a tight bound — the mechanism #77 actually fixes.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="killpg/setsid/process groups are POSIX-only"
)

FIXTURES = Path(__file__).parent / "fixtures"
DRIVER_SCRIPT = FIXTURES / "sigint_driver.py"
HARNESS_SCRIPT = FIXTURES / "sigint_harness_stub.py"

_HARNESS_DEATH_BUDGET = 3.0   # tight bound: fixed code kills the harness in ms
_DRIVER_EXIT_BUDGET = 10.0    # generous: covers cleanup + process teardown
_SETTLE = 0.3                 # let the harness finish installing its SIGINT handler


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was never created within {timeout}s")


def _wait_for_pid_gone(pid: int, timeout: float) -> bool:
    """Poll os.kill(pid, 0) until it raises ProcessLookupError. Returns True if gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class _Rig:
    """Isolated env + file layout for one driver+harness run."""

    def __init__(self, tmp_path: Path, mode: str, harness_sleep: float = 25.0, exit_code: int = 0):
        self.tmp_path = tmp_path
        self.iso_home = tmp_path / "home"
        self.iso_tmp = tmp_path / "tmp"
        self.iso_home.mkdir()
        self.iso_tmp.mkdir()

        self.pid_file = tmp_path / "harness.pid"
        self.marker_file = tmp_path / "harness.marker"
        self.port_file = tmp_path / "ctrl.port"
        self.log_file = tmp_path / "driver.log"

        self.env = {
            **os.environ,
            "HOME": str(self.iso_home),
            "TMPDIR": str(self.iso_tmp),
            "TOMMY_TEST_HARNESS_SCRIPT": str(HARNESS_SCRIPT),
            "TOMMY_TEST_HARNESS_PIDFILE": str(self.pid_file),
            "TOMMY_TEST_HARNESS_MARKERFILE": str(self.marker_file),
            "TOMMY_TEST_CTRL_PORT_FILE": str(self.port_file),
            "TOMMY_TEST_HARNESS_MODE": mode,
            "TOMMY_TEST_HARNESS_SLEEP": str(harness_sleep),
            "TOMMY_TEST_HARNESS_EXITCODE": str(exit_code),
        }

        self._log_fh = open(self.log_file, "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(DRIVER_SCRIPT)],
            env=self.env,
            cwd=str(self.iso_home),
            start_new_session=True,  # new session + new pgid == proc.pid
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )

    def diag(self) -> str:
        self._log_fh.flush()
        try:
            return self.log_file.read_text()
        except OSError:
            return "<no driver log>"

    def close(self) -> None:
        _kill_process_group(self.proc)
        # Belt-and-suspenders: on the pre-#77 bug shape the harness lands in
        # its own process group, so killing the driver's group above never
        # reaches it. Try the harness pid directly too.
        if self.pid_file.exists():
            try:
                os.kill(int(self.pid_file.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError, ValueError):
                pass
        self._log_fh.close()


@pytest.fixture
def rig(tmp_path):
    created: list[_Rig] = []

    def _make(mode: str, **kwargs) -> _Rig:
        r = _Rig(tmp_path, mode, **kwargs)
        created.append(r)
        return r

    yield _make
    for r in created:
        r.close()


# ---------------------------------------------------------------------------
# SIGINT path — contracts 1-3
# ---------------------------------------------------------------------------

def test_sigint_to_process_group_kills_harness_and_cleans_up(rig):
    r = rig("sleep", harness_sleep=25.0)

    _wait_for_file(r.pid_file, timeout=10)
    harness_pid = int(r.pid_file.read_text().strip())
    time.sleep(_SETTLE)  # let the harness finish installing its SIGINT handler

    # Precondition: the driver really is its own process-group leader, and
    # the harness (Popen'd without start_new_session, per the #77 fix)
    # inherited that same group — otherwise killpg below wouldn't be
    # testing what it claims to.
    assert os.getpgid(r.proc.pid) == r.proc.pid, r.diag()
    assert os.getpgid(harness_pid) == r.proc.pid, (
        "harness landed in a different process group than the driver — "
        "start_new_session must have reappeared on the harness Popen call\n"
        + r.diag()
    )

    prompt_matches = list(r.iso_tmp.glob("tommy-prompt-*"))
    assert len(prompt_matches) == 1, (
        f"expected exactly one tommy-prompt-* temp file before signaling, "
        f"got {prompt_matches}\n{r.diag()}"
    )
    prompt_file = prompt_matches[0]

    _wait_for_file(r.port_file, timeout=5)
    ctrl_port = int(r.port_file.read_text().strip())

    # The Ctrl-C: signal the whole process group, not just the driver.
    os.killpg(r.proc.pid, signal.SIGINT)

    # Contract: no orphaned harness surviving the signal — checked on a
    # tight bound, independent of whether/when the driver's own wait()
    # returns. Fixed code kills this in milliseconds; the pre-#77 shape
    # (harness in its own session) would still be alive here.
    harness_dead = _wait_for_pid_gone(harness_pid, timeout=_HARNESS_DEATH_BUDGET)
    assert harness_dead, (
        f"harness pid {harness_pid} still alive {_HARNESS_DEATH_BUDGET}s after "
        f"SIGINT was sent to the process group — orphaned harness\n{r.diag()}"
    )

    try:
        r.proc.wait(timeout=_DRIVER_EXIT_BUDGET)
    except subprocess.TimeoutExpired:
        pytest.fail(f"driver did not exit within {_DRIVER_EXIT_BUDGET}s of SIGINT\n{r.diag()}")

    assert r.marker_file.read_text() == "sigint-received", (
        "harness marker does not show it received/handled SIGINT "
        f"(got {r.marker_file.read_text()!r}) — harness may have died "
        f"for an unrelated reason, or never received the signal at all\n{r.diag()}"
    )

    assert not prompt_file.exists(), (
        f"prompt file {prompt_file} was not cleaned up after SIGINT\n{r.diag()}"
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        with pytest.raises(ConnectionRefusedError):
            sock.connect(("127.0.0.1", ctrl_port))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Normal exit path — contract 4 (no regression to PR #77's contract 4)
# ---------------------------------------------------------------------------

def test_clean_exit_returns_harness_exit_code(rig):
    r = rig("exit", exit_code=7)

    try:
        r.proc.wait(timeout=_DRIVER_EXIT_BUDGET)
    except subprocess.TimeoutExpired:
        pytest.fail(f"driver did not exit within {_DRIVER_EXIT_BUDGET}s on a clean run\n{r.diag()}")

    _wait_for_file(r.pid_file, timeout=1)  # sanity: the stub actually ran

    assert r.proc.returncode == 7, (
        f"expected tommy's exit code to reflect the harness's returncode (7), "
        f"got {r.proc.returncode}\n{r.diag()}"
    )

    prompt_matches = list(r.iso_tmp.glob("tommy-prompt-*"))
    assert prompt_matches == [], (
        f"prompt temp file(s) left behind after a clean exit: {prompt_matches}\n{r.diag()}"
    )

    _wait_for_file(r.port_file, timeout=5)
    ctrl_port = int(r.port_file.read_text().strip())
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        with pytest.raises(ConnectionRefusedError):
            sock.connect(("127.0.0.1", ctrl_port))
    finally:
        sock.close()
