"""issue #68 — the OTHER real stdio launch path: `python memnos_cli.py mcp` (the
fallback _mcp_launcher() uses when no `memnos` console-script is on PATH — see
memnos_cli.py's own docstring on _mcp_launcher) -> cmd_mcp() -> memnos_mcp.run_stdio().

This matters specifically because the PR's central design deviation — using
os.execv(sys.executable, [sys.executable, *argv]) instead of the originally-proposed
os.execv(sys.argv[0], sys.argv) — was justified by this exact launch shape:
memnos_cli.py has no shebang line and isn't marked executable, so the naive form
raises PermissionError here (verified directly with a standalone repro; see the PR
description). test_mcp_adapter_reexec.py only ever launches memnos_mcp.py directly,
which never exercises cmd_mcp(), memnos_mcp.run_stdio()'s call site in memnos_cli.py,
or _resolve_reexec_argv0()'s non-executable/non-absolute argv[0] branches. This test
launches with a RELATIVE script path + cwd=install_dir (`cd install && python
memnos_cli.py mcp`, the literal fallback shape a human or a PATH-less host would
produce) specifically so sys.argv[0] inside the child is the bare relative string
"memnos_cli.py", not an already-absolute path — closing that gap.

Run: python tests/test_mcp_adapter_reexec_cli_launch.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _reexec_test_helpers as h

PASS = FAIL = 0
NS = "test:mcp-adapter-reexec-cli-launch"
TOKEN = "mnk_reexec_cli_launch_token"
POLL_DEADLINE_S = 30


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def main():
    srv, url = h.start_stub(h.make_stub_handler())
    tmp_root = tempfile.mkdtemp(prefix="memnos_reexec_cli_")
    install_dir = h.make_fake_install(tmp_root, with_cli=True)
    home = os.path.join(tmp_root, "home")
    os.makedirs(home, exist_ok=True)

    env = h.build_env(url, TOKEN, NS, home, interval="1", grace="0.2")
    # RELATIVE "memnos_cli.py" + cwd=install_dir — NOT an absolute path. This is the
    # branch of _resolve_reexec_argv0 that the direct-memnos_mcp.py launch never
    # touches (that one always gets an already-absolute argv[0]).
    rpc = h.StdioRPC("memnos_cli.py", env, cwd=install_dir, extra_args=["mcp"])

    exit_seen = {"value": False}

    def _poll_forever():
        while not stop_poll.is_set():
            if rpc.proc.poll() is not None:
                exit_seen["value"] = True
            time.sleep(0.02)

    stop_poll = threading.Event()
    poller = threading.Thread(target=_poll_forever, daemon=True)
    poller.start()

    try:
        print("=== launched via `python memnos_cli.py mcp` (cmd_mcp -> run_stdio) ===")
        rpc.initialize()
        pid0 = rpc.pid
        check("subprocess is alive right after initialize", rpc.alive())

        remember_resp = rpc.call_tool("remember", {"text": "cli-launch baseline fact CL-4004"})
        check("remember() works when launched through cmd_mcp (not just memnos_mcp.py directly)",
              not h.tool_is_error(remember_resp), h.tool_text(remember_resp))

        print("=== simulating `memnos upgrade` under THIS launch path ===")
        h.simulate_upgrade(install_dir)

        print(f"=== waiting up to {POLL_DEADLINE_S}s for re-exec (proves sys.executable "
              f"form works here, where the naive os.execv(sys.argv[0], sys.argv) form "
              f"would have raised PermissionError) ===")
        deadline = time.time() + POLL_DEADLINE_S
        marker_after = None
        while time.time() < deadline:
            resp = rpc.call_tool("_reexec_marker", {}, timeout=5)
            if not h.tool_is_error(resp):
                marker_after = resp
                break
            time.sleep(0.5)

        check("the marker tool succeeds after the simulated upgrade — re-exec worked "
              "via the cmd_mcp/relative-argv0 launch path",
              marker_after is not None and h.tool_text(marker_after) == "REEXEC_MARKER_PRESENT",
              h.tool_text(marker_after or {}))
        check("same pid throughout", rpc.pid == pid0)
        check("the parent never observed the child exit (execv, not a crash+respawn)",
              exit_seen["value"] is False)

        stderr = rpc.stderr_text()
        check("stderr never shows a failed re-exec (e.g. PermissionError) — the "
              "sys.executable form succeeded where the naive form would not have",
              "re-exec failed" not in stderr and "PermissionError" not in stderr,
              stderr[-2000:])
        check("stderr logs the successful re-exec with the recorded pid",
              f"re-exec'ing adapter (pid {pid0})" in stderr, stderr[-2000:])

        recall_resp = rpc.call_tool("recall", {"query": "cli-launch baseline"})
        check("recall() still works post-reexec on the same connection",
              not h.tool_is_error(recall_resp), h.tool_text(recall_resp))

    finally:
        stop_poll.set()
        rpc.close()
        srv.shutdown()
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
