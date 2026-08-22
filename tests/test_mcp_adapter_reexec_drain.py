"""issue #68 — idle-only re-exec: a version change detected WHILE an MCP tool call is
in flight must NOT swap the process image until that call has actually completed and
its response has reached the caller. Real subprocess, real JSON-RPC.

The in-flight call is made genuinely slow by pointing MEMNOS_URL at a stub HTTP server
whose /remember handler sleeps SLOW_DELAY_S before answering — memnos_mcp.remember()
is a thin forwarder, so the tool call is provably still executing for that whole
window (this is NOT a mock of memnos_mcp's own logic, the real remember() code runs
against a real, just-slow, upstream).

Timeline hammered by this test:
  T0  fire remember() WITHOUT waiting for its response (holds the adapter's in-flight
      counter at >=1 for SLOW_DELAY_S seconds)
  T0+ simulate the upgrade immediately — well inside the slow window
  T0..T_complete  repeatedly confirm, WHILE the slow call is still outstanding, that no
      re-exec has happened yet (the marker tool still fails) — this is issued as a
      SECOND, concurrent request over the SAME connection (JSON-RPC over stdio
      natively supports concurrent in-flight requests by id; this is not a serialized
      queue), proving the watcher really did detect the change mid-flight and is
      actively holding off, not just "hasn't gotten around to checking yet"
  T_complete  the slow remember() call finally returns — content is checked byte-exact
      against the stub's canned response, proving it was neither dropped nor corrupted
      by whatever the watcher was doing concurrently
  T_complete..  the marker tool starts succeeding shortly after — the re-exec that was
      deferred actually happens once idle

Run: python tests/test_mcp_adapter_reexec_drain.py
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
NS = "test:mcp-adapter-reexec-drain"
TOKEN = "mnk_reexec_drain_token"
SLOW_DELAY_S = 4.0
SENTINEL_TURN_ID = 987654321
POLL_DEADLINE_S = 30


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def main():
    srv, url = h.start_stub(h.make_stub_handler(remember_delay=SLOW_DELAY_S,
                                                  remember_turn_id=SENTINEL_TURN_ID))
    tmp_root = tempfile.mkdtemp(prefix="memnos_reexec_drain_")
    install_dir = h.make_fake_install(tmp_root)
    home = os.path.join(tmp_root, "home")
    os.makedirs(home, exist_ok=True)

    # short interval so the watcher's next tick lands well inside the SLOW_DELAY_S window
    env = h.build_env(url, TOKEN, NS, home, interval="1", grace="0.2")
    rpc = h.StdioRPC(os.path.join(install_dir, "memnos_mcp.py"), env)

    slow_result: dict = {}

    def _run_slow_call():
        slow_result["resp"] = rpc.call_tool(
            "remember", {"text": "drain-safety in-flight fact DS-3003"},
            timeout=SLOW_DELAY_S + 15)
        slow_result["done_at"] = time.time()

    try:
        print("=== baseline handshake ===")
        rpc.initialize()
        pid0 = rpc.pid

        print(f"=== firing a SLOW remember() (upstream sleeps {SLOW_DELAY_S}s) — holds "
              f"the in-flight counter up while we trigger the upgrade ===")
        t0 = time.time()
        slow_thread = threading.Thread(target=_run_slow_call, daemon=True)
        slow_thread.start()
        time.sleep(0.3)                      # let the request actually reach the server

        print("=== simulating `memnos upgrade` WHILE that call is still in flight ===")
        h.simulate_upgrade(install_dir)

        print("=== while the slow call is outstanding: repeatedly prove NO re-exec yet ===")
        no_premature_reexec = True
        checked_while_in_flight = 0
        while slow_thread.is_alive() and (time.time() - t0) < SLOW_DELAY_S - 0.3:
            marker_resp = rpc.call_tool("_reexec_marker", {}, timeout=5)
            checked_while_in_flight += 1
            if not h.tool_is_error(marker_resp):
                no_premature_reexec = False
                break
            time.sleep(0.3)

        check("actually got to probe mid-flight at least once (test isn't vacuously true)",
              checked_while_in_flight > 0, str(checked_while_in_flight))
        check("no re-exec happened while the slow tool call was still in flight — the "
              "watcher detected the change but deferred the swap",
              no_premature_reexec)
        check("the slow call's own subprocess is still alive throughout (not dropped)",
              rpc.alive())

        print("=== waiting for the slow call to actually complete ===")
        slow_thread.join(timeout=SLOW_DELAY_S + 15)
        check("the slow remember() call eventually completed",
              "resp" in slow_result, "thread never finished")
        resp = slow_result.get("resp", {})
        check("the in-flight call completed successfully (not dropped)",
              not h.tool_is_error(resp), h.tool_text(resp))
        check("the in-flight call's response is byte-exact vs the stub's canned reply "
              "(not corrupted by a concurrent process-image swap)",
              f"turn {SENTINEL_TURN_ID}" in h.tool_text(resp), h.tool_text(resp))
        check("the slow call took at least SLOW_DELAY_S (proves it really was in-flight, "
              "not short-circuited)",
              (slow_result["done_at"] - t0) >= (SLOW_DELAY_S - 0.5),
              f"{slow_result['done_at'] - t0:.2f}s")

        print(f"=== the deferred re-exec now proceeds (waiting up to {POLL_DEADLINE_S}s) ===")
        deadline = time.time() + POLL_DEADLINE_S
        marker_after = None
        while time.time() < deadline:
            r = rpc.call_tool("_reexec_marker", {}, timeout=5)
            if not h.tool_is_error(r):
                marker_after = r
                break
            time.sleep(0.4)
        check("the re-exec proceeds once idle: the marker tool now succeeds",
              marker_after is not None and h.tool_text(marker_after) == "REEXEC_MARKER_PRESENT",
              h.tool_text(marker_after or {}))

        check("the deferred re-exec preserved the pid (same execv guarantee as the "
              "non-contended case)", rpc.pid == pid0)

        stderr = rpc.stderr_text()
        check("stderr shows the watcher noticed the change before it actually swapped "
              "(i.e. detection and swap are logged as two distinct moments)",
              "will re-exec" in stderr and "re-exec'ing adapter" in stderr,
              stderr[-2000:])

    finally:
        rpc.close()
        srv.shutdown()
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
