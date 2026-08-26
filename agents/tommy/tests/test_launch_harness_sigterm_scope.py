"""
Real runtime behavioral test for the cli.py _launch_harness SIGTERM fix,
part of a blocking finding from an adversarial review of
tommy/memnos_scope.py (post-#136 landing): only KeyboardInterrupt (Ctrl-C)
was ever caught around proc.wait() there, so a SIGTERM sent directly to the
interactive `tommy` CLI process's own PID (as distinct from Ctrl-C, which the
terminal delivers to the whole foreground process group) hit Python's
default SIGTERM disposition — process termination WITHOUT running any
`finally` block — and could skip ScopingFiles.cleanup() entirely, leaving
the dispatch-scoped .mcp.json / .claude/settings.local.json unrestored.

Mirrors test_launch_harness_sigint.py's proof strategy deliberately: a real
"tommy" stand-in subprocess (fixtures/sigint_driver.py, reused as-is — the
scoping decision happens entirely inside cli.py before the harness is even
Popen'd, so the SAME driver/harness-stub pair issue #77's SIGINT test
already uses works unmodified here) running the REAL, unmodified
_launch_harness() end-to-end, signaled with a real `os.kill(pid,
signal.SIGTERM)` — NOT os.killpg() — sent to just the driver's own PID, the
specific gap the review named.

Two things are asserted, matching the task's acceptance criteria plus the
stronger guarantee this fix actually provides:
  1. The workspace is left in a SAFE state — never a torn/partially-written
     file (memnos_scope.py's own reference-counted, lock-serialized,
     hash-verified design already guarantees this independent of any signal
     handler — see its module docstring).
  2. Because cli.py now installs a scoped SIGTERM handler around
     proc.wait() that converts SIGTERM into a catchable exception routed
     through the SAME finally block KeyboardInterrupt already uses, the
     workspace is actually restored to its EXACT original .mcp.json bytes
     by the time the driver process exits — not just "eventually self-heals
     on some later dispatch," the fallback memnos_scope.py's design commits
     to only when nothing can be hooked at all (e.g. a real SIGKILL).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="signal delivery semantics tested here are POSIX-only"
)

FIXTURES = Path(__file__).parent / "fixtures"
DRIVER_SCRIPT = FIXTURES / "sigint_driver.py"
HARNESS_SCRIPT = FIXTURES / "sigint_harness_stub.py"

_DRIVER_EXIT_BUDGET = 10.0


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was never created within {timeout}s")


def _fake_memnos_stub(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "memnos"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)


class _ScopedRig:
    """Same shape as test_launch_harness_sigint.py's _Rig, plus the
    workspace setup should_scope_dispatch() needs to actually activate
    scoping: an explicit tommy.yaml memnos.namespace, no existing binding
    (iso_home is a throwaway $HOME with no bindings_cache.json), and a
    `memnos` binary resolvable on PATH."""

    def __init__(self, tmp_path: Path, *, pre_existing_mcp_json: dict | None = None,
                 harness_sleep: float = 25.0):
        self.tmp_path = tmp_path
        self.iso_home = tmp_path / "home"
        self.iso_tmp = tmp_path / "tmp"
        self.iso_home.mkdir()
        self.iso_tmp.mkdir()

        (self.iso_home / "tommy.yaml").write_text(
            "tommy:\n  version: 1\nmemnos:\n  namespace: test:sigterm-scope\n"
        )
        self.mcp_json_path = self.iso_home / ".mcp.json"
        self.original_mcp_json_text = None
        if pre_existing_mcp_json is not None:
            self.original_mcp_json_text = json.dumps(pre_existing_mcp_json, indent=2) + "\n"
            self.mcp_json_path.write_text(self.original_mcp_json_text)

        bin_dir = tmp_path / "fakebin"
        _fake_memnos_stub(bin_dir)

        self.pid_file = tmp_path / "harness.pid"
        self.marker_file = tmp_path / "harness.marker"
        self.ready_file = tmp_path / "harness.ready"
        self.port_file = tmp_path / "ctrl.port"
        self.log_file = tmp_path / "driver.log"

        self.env = {
            **os.environ,
            "HOME": str(self.iso_home),
            "TMPDIR": str(self.iso_tmp),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "TOMMY_TEST_HARNESS_SCRIPT": str(HARNESS_SCRIPT),
            "TOMMY_TEST_HARNESS_PIDFILE": str(self.pid_file),
            "TOMMY_TEST_HARNESS_MARKERFILE": str(self.marker_file),
            "TOMMY_TEST_HANDLER_READY_FILE": str(self.ready_file),
            "TOMMY_TEST_CTRL_PORT_FILE": str(self.port_file),
            "TOMMY_TEST_HARNESS_MODE": "sleep",
            "TOMMY_TEST_HARNESS_SLEEP": str(harness_sleep),
            "TOMMY_TEST_HARNESS_EXITCODE": "0",
        }
        self.env.pop("MEMNOS_NS", None)

        self._log_fh = open(self.log_file, "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(DRIVER_SCRIPT)],
            env=self.env,
            cwd=str(self.iso_home),
            start_new_session=True,
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
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if self.pid_file.exists():
            try:
                os.kill(int(self.pid_file.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError, ValueError):
                pass
        self._log_fh.close()


@pytest.fixture
def scoped_rig(tmp_path):
    created: list[_ScopedRig] = []

    def _make(**kwargs) -> _ScopedRig:
        r = _ScopedRig(tmp_path, **kwargs)
        created.append(r)
        return r

    yield _make
    for r in created:
        r.close()


def test_sigterm_to_driver_pid_restores_exact_original_workspace(scoped_rig):
    """The named gap: SIGTERM sent to the tommy CLI process's OWN pid (not
    the process group — os.kill, not os.killpg) while it is mid-dispatch,
    scoping already active, well before the harness's own sleep would ever
    let proc.wait() return normally."""
    pre_existing = {"mcpServers": {"other_server": {"command": "other", "args": []}}}
    r = scoped_rig(pre_existing_mcp_json=pre_existing, harness_sleep=25.0)

    _wait_for_file(r.pid_file, timeout=10)
    _wait_for_file(r.ready_file, timeout=10)  # harness stub's SIGINT handler installed (irrelevant here, but a reliable "harness is up" signal)
    _wait_for_file(r.port_file, timeout=5)

    # Scoping must actually have activated before we signal, or this test
    # would trivially pass without exercising anything — the merged file
    # must exist and carry the memnos block right now.
    assert r.mcp_json_path.exists(), f"scoping never generated .mcp.json\n{r.diag()}"
    scoped_text = r.mcp_json_path.read_text()
    scoped_data = json.loads(scoped_text)
    assert "memnos" in scoped_data["mcpServers"], f"scoping did not activate\n{r.diag()}"
    assert "other_server" in scoped_data["mcpServers"], f"pre-existing server lost in merge\n{r.diag()}"

    settings_local_path = r.iso_home / ".claude" / "settings.local.json"
    assert settings_local_path.exists(), f"settings.local.json was not generated\n{r.diag()}"

    # The named gap, reproduced: SIGTERM to just this process's pid.
    os.kill(r.proc.pid, signal.SIGTERM)

    try:
        r.proc.wait(timeout=_DRIVER_EXIT_BUDGET)
    except subprocess.TimeoutExpired:
        pytest.fail(f"driver did not exit within {_DRIVER_EXIT_BUDGET}s of SIGTERM\n{r.diag()}")

    # The exit code itself isn't the contract under test here (it reflects
    # whatever the forwarded-to harness child's own returncode ended up
    # being, e.g. a signal-terminated child encodes as 256-N — plumbing,
    # not a workspace-safety property) — the load-bearing checks are that
    # the driver actually exited well within budget (proven above: no
    # TimeoutExpired) and the workspace state assertions below.

    # Safety, at minimum: valid JSON, never a torn/partial write.
    final_text = r.mcp_json_path.read_text()
    final_data = json.loads(final_text)  # raises if torn/corrupted — the real assertion

    # The stronger guarantee this fix actually provides: exact original
    # restored synchronously, not left in the (also-safe) scoped state
    # waiting for a future dispatch to self-heal it.
    assert final_text == r.original_mcp_json_text, (
        f"SIGTERM did not trigger synchronous cleanup — .mcp.json left scoped "
        f"instead of restored to its exact original bytes\ngot: {final_text}\n{r.diag()}"
    )
    assert not settings_local_path.exists(), (
        f"settings.local.json (which did not exist before this dispatch) was not "
        f"cleaned up after SIGTERM\n{r.diag()}"
    )

    # Belt-and-suspenders: no orphaned harness left running, mirroring
    # test_launch_harness_sigint.py's equivalent check.
    if r.pid_file.exists():
        harness_pid = int(r.pid_file.read_text().strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(harness_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"harness pid {harness_pid} still alive after driver exit\n{r.diag()}")


def test_sigterm_before_any_scoping_state_still_exits_cleanly(scoped_rig):
    """Sanity/regression companion: a workspace with NO pre-existing
    .mcp.json (the common case — scoping creates it fresh) must also come
    back to "does not exist" after SIGTERM, not merely "does not error"."""
    r = scoped_rig(pre_existing_mcp_json=None, harness_sleep=25.0)

    _wait_for_file(r.pid_file, timeout=10)
    _wait_for_file(r.ready_file, timeout=10)
    _wait_for_file(r.port_file, timeout=5)
    assert r.mcp_json_path.exists(), f"scoping never generated .mcp.json\n{r.diag()}"

    os.kill(r.proc.pid, signal.SIGTERM)
    try:
        r.proc.wait(timeout=_DRIVER_EXIT_BUDGET)
    except subprocess.TimeoutExpired:
        pytest.fail(f"driver did not exit within {_DRIVER_EXIT_BUDGET}s of SIGTERM\n{r.diag()}")

    assert not r.mcp_json_path.exists(), (
        f"a freshly-generated (no prior file) .mcp.json must be REMOVED on cleanup, "
        f"not left behind, after SIGTERM\n{r.diag()}"
    )
