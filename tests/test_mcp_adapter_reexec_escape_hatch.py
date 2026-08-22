"""issue #68 — the escape hatch: MEMNOS_ADAPTER_REEXEC=0 must fully disable the
self-re-exec watcher, reverting to today's manual-restart behavior. Real subprocess,
same simulated-upgrade technique as test_mcp_adapter_reexec.py (a marker tool that
exists only in the "new" build) — but this time the affirmative proof is that the
marker tool NEVER starts working, even after waiting through many watcher intervals
that WOULD have triggered a swap with the flag left at its default.

Also proves the adapter still starts up and serves normal tool calls perfectly well
with the flag set — this isn't "the flag breaks something", it's "the flag opts out of
one specific background behavior".

Run: python tests/test_mcp_adapter_reexec_escape_hatch.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _reexec_test_helpers as h

PASS = FAIL = 0
NS = "test:mcp-adapter-reexec-escape-hatch"
TOKEN = "mnk_reexec_escape_hatch_token"
INTERVAL_S = 1
WAIT_S = 6            # several multiples of INTERVAL_S — long enough that a live watcher
                       # would have swapped by now (proven in test_mcp_adapter_reexec.py)


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def main():
    srv, url = h.start_stub(h.make_stub_handler())
    tmp_root = tempfile.mkdtemp(prefix="memnos_reexec_escape_")
    install_dir = h.make_fake_install(tmp_root)
    home = os.path.join(tmp_root, "home")
    os.makedirs(home, exist_ok=True)

    env = h.build_env(url, TOKEN, NS, home, interval=str(INTERVAL_S), grace="0.2", reexec="0")
    rpc = h.StdioRPC(os.path.join(install_dir, "memnos_mcp.py"), env)

    try:
        print("=== MEMNOS_ADAPTER_REEXEC=0: adapter still starts and serves normally ===")
        rpc.initialize()
        pid0 = rpc.pid
        remember_resp = rpc.call_tool("remember", {"text": "escape-hatch baseline fact EH-2002"})
        check("remember() still works with the watcher disabled",
              not h.tool_is_error(remember_resp), h.tool_text(remember_resp))

        stderr = rpc.stderr_text()
        check("adapter logs that the watcher is disabled at startup",
              "MEMNOS_ADAPTER_REEXEC=0" in stderr and "disabled" in stderr, stderr[-500:])

        print("=== simulating `memnos upgrade` — a live watcher WOULD swap on this ===")
        h.simulate_upgrade(install_dir)

        print(f"=== waiting {WAIT_S}s (>= {WAIT_S // INTERVAL_S} watcher intervals) ===")
        time.sleep(WAIT_S)

        marker_resp = rpc.call_tool("_reexec_marker", {})
        check("the marker tool from the simulated upgrade NEVER appears — no re-exec occurred",
              h.tool_is_error(marker_resp) and "Unknown tool" in h.tool_text(marker_resp),
              h.tool_text(marker_resp))

        check("the subprocess pid is unchanged (never re-exec'd)", rpc.pid == pid0)
        check("the subprocess is still alive and healthy (disabling the watcher doesn't "
              "crash the adapter, it just opts out of the swap)", rpc.alive())

        stderr2 = rpc.stderr_text()
        check("stderr never logs a re-exec", "re-exec'ing adapter" not in stderr2, stderr2[-2000:])

        print("=== old tools keep working fine the whole time (flag doesn't break normal use) ===")
        recall_resp = rpc.call_tool("recall", {"query": "escape-hatch baseline"})
        check("recall() still works after the (skipped) upgrade window",
              not h.tool_is_error(recall_resp), h.tool_text(recall_resp))

    finally:
        rpc.close()
        srv.shutdown()
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
