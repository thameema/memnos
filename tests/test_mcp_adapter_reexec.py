"""issue #68 — the core acceptance test: with a stdio MCP session LIVE, an on-disk
`memnos upgrade` (simulated here by mutating a copy of memnos_mcp.py under the running
process) makes the adapter re-exec itself IN PLACE — same PID, same stdin/stdout pipes
already connected to the "host" (this test), no dropped connection, no
FileNotFoundError, no manual session restart — and the next tool call runs the NEW
code, not just the old code with a coincidentally-unchanged pid.

Real subprocess (subprocess.Popen, not a mock), real JSON-RPC over its actual stdin/
stdout (see tests/_reexec_test_helpers.StdioRPC — deliberately NOT the mcp SDK's own
stdio_client(), which spawns its subprocess internally and never exposes the resulting
process object). No Postgres needed: memnos_mcp.py is a thin adapter that forwards to
whatever MEMNOS_URL points at, so a minimal stub HTTP server stands in for the real
memnos server (same pattern as tests/test_write_behind_stdio_token_regression.py).

Proof shape, each checked independently rather than inferred from one signal:
  - the SAME subprocess.Popen handle's `poll()` NEVER returns non-None at any point
    sampled throughout the test (continuously polled on a background thread) — this is
    the sharpest available proof that execv() happened rather than a fork/kill/restart:
    an execve() call generates no process-exit event at all, by construction.
  - a NEW tool (`_reexec_marker`) that exists ONLY in the "upgraded" copy fails with
    "Unknown tool" before the swap and succeeds with its exact marker text after —
    proof the NEW CODE is genuinely running, not just that a log line printed.
  - the pre-upgrade ClientSession/pipes keep working afterward with ZERO reconnect:
    the same StdioRPC instance, same stdin/stdout file objects, used for every call in
    this test, before and after the swap.
  - exactly one re-exec occurs even after waiting through several more watcher
    intervals post-swap (no re-exec storm — the fresh process's own signature now
    matches what's on disk).

Run: python tests/test_mcp_adapter_reexec.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _reexec_test_helpers as h

PASS = FAIL = 0
NS = "test:mcp-adapter-reexec"
TOKEN = "mnk_reexec_test_token"
POLL_DEADLINE_S = 30          # generous — CI runners are noticeably slower than dev boxes


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def main():
    srv, url = h.start_stub(h.make_stub_handler())
    tmp_root = tempfile.mkdtemp(prefix="memnos_reexec_")
    install_dir = h.make_fake_install(tmp_root)
    home = os.path.join(tmp_root, "home")
    os.makedirs(home, exist_ok=True)

    env = h.build_env(url, TOKEN, NS, home, interval="1", grace="0.2")
    rpc = h.StdioRPC(os.path.join(install_dir, "memnos_mcp.py"), env)

    # a background poller that NEVER stops sampling — if execv genuinely happened, this
    # must never once observe a non-None returncode, at ANY point in the test.
    exit_seen = {"value": False}

    def _poll_forever():
        while not stop_poll.is_set():
            if rpc.proc.poll() is not None:
                exit_seen["value"] = True
            time.sleep(0.02)

    import threading
    stop_poll = threading.Event()
    poller = threading.Thread(target=_poll_forever, daemon=True)
    poller.start()

    try:
        print("=== baseline: real stdio subprocess, real JSON-RPC handshake ===")
        rpc.initialize()
        pid0 = rpc.pid
        check("subprocess is alive right after initialize", rpc.alive())

        remember_resp = rpc.call_tool("remember", {"text": "the reexec baseline fact is RB-1001"})
        check("remember() succeeds over real stdio JSON-RPC (pre-upgrade)",
              not h.tool_is_error(remember_resp), h.tool_text(remember_resp))

        marker_before = rpc.call_tool("_reexec_marker", {})
        check("the marker tool from the simulated upgrade does NOT exist yet (old code)",
              h.tool_is_error(marker_before) and "Unknown tool" in h.tool_text(marker_before),
              h.tool_text(marker_before))

        print("=== simulating `memnos upgrade` swapping files under the running process ===")
        h.simulate_upgrade(install_dir)

        print(f"=== waiting up to {POLL_DEADLINE_S}s for the adapter to notice + re-exec ===")
        deadline = time.time() + POLL_DEADLINE_S
        marker_after = None
        while time.time() < deadline:
            resp = rpc.call_tool("_reexec_marker", {}, timeout=5)
            if not h.tool_is_error(resp):
                marker_after = resp
                break
            time.sleep(0.5)

        check("the marker tool from the simulated upgrade NOW succeeds (new code is running)",
              marker_after is not None and h.tool_text(marker_after) == "REEXEC_MARKER_PRESENT",
              h.tool_text(marker_after or {}))

        check("the subprocess pid never changed (same Popen handle throughout — execv "
              "preserves the OS pid by definition; this is the same variable the whole "
              "test, not re-discovered)", rpc.pid == pid0, f"{rpc.pid} vs {pid0}")

        check("the parent NEVER observed the child exit at any sampled point (poll() "
              "stayed None throughout) — the key proof this was execv, not a "
              "fork/kill/restart, which WOULD produce a visible exit event",
              exit_seen["value"] is False)

        stderr = rpc.stderr_text()
        check("stderr logs the re-exec with the pid the parent recorded",
              f"re-exec'ing adapter (pid {pid0})" in stderr, stderr[-2000:])

        print("=== SAME pipes, SAME StdioRPC instance, zero reconnect: old tools still work ===")
        recall_resp = rpc.call_tool("recall", {"query": "reexec baseline"})
        check("recall() still works post-reexec on the SAME connection (no reconnect)",
              not h.tool_is_error(recall_resp), h.tool_text(recall_resp))

        print("=== no re-exec storm: waiting through several more watcher intervals ===")
        time.sleep(4)          # several more 1s ticks past the swap
        check("subprocess is still alive (no crash-loop) after waiting past the swap",
              rpc.alive() and not exit_seen["value"])
        stderr2 = rpc.stderr_text()
        check("exactly ONE re-exec happened — the post-swap process's own signature now "
              "matches what's on disk, so it never re-triggers itself",
              stderr2.count("re-exec'ing adapter") == 1, stderr2[-2000:])

    finally:
        stop_poll.set()
        rpc.close()
        srv.shutdown()
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
