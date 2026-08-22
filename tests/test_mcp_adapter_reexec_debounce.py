"""issue #68 — torn-upgrade debounce: `pip install -U memnos` / `uv tool install
--force memnos` rewrites several files over some span of real time, not atomically.
Acting on the FIRST observed change to _version_signature() risks re-exec'ing into a
half-written tree (memnos_mcp.py's new mtime landing while memnos_cli.py / core/ are
still mid-write or briefly missing) — the freshly exec'd process would crash on
import, and the MCP host would see exactly the exit/restart this issue exists to
eliminate, just relocated to a new moment instead of prevented.

_reexec_watch_loop's fix: don't act on the first change — require the signature to
read back UNCHANGED on two consecutive ticks before treating it as "settled" and
handing off to _wait_idle_and_reexec(). This test drives that loop function directly
(in-process, not a subprocess) with a scripted, deterministic sequence of
_version_signature() return values simulating: stable start -> torn/moving -> torn
again/still moving (different value) -> settled on the FINAL value -> fires.
time.sleep is stubbed to a no-op (this test asserts on CALL SEQUENCE, not wall clock)
with a hard call-count safety cap so a broken debounce (loops forever without firing)
fails fast instead of hanging.

Run: python tests/test_mcp_adapter_reexec_debounce.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


class _FakeTime:
    """Stand-in for the stdlib `time` module as memnos_mcp.py sees it (module-level
    `import time`, rebound per-test via `memnos_mcp.time = ...` — this only shadows the
    name inside memnos_mcp's own namespace, not the real time module used elsewhere).
    sleep() is a no-op (this test is about call ORDER, not real elapsed time) with a
    hard cap: a debounce implementation that never settles would otherwise spin
    forever with zero real delay between iterations."""
    MAX_SLEEPS = 25

    def __init__(self):
        self.sleep_calls = 0

    def sleep(self, _seconds):
        self.sleep_calls += 1
        if self.sleep_calls > self.MAX_SLEEPS:
            raise RuntimeError(
                f"safety cap: _reexec_watch_loop looped {self.sleep_calls} times "
                f"without settling/firing — debounce logic is stuck or broken")


def main():
    home = tempfile.mkdtemp(prefix="memnos_reexec_debounce_")
    os.environ["HOME"] = home
    os.environ["MEMNOS_URL"] = "http://127.0.0.1:1"   # never actually contacted
    os.environ["MEMNOS_TOKEN"] = "mnk_debounce_test"
    os.environ["MEMNOS_NS"] = "test:mcp-adapter-reexec-debounce"
    os.environ.pop("OPENAI_API_KEY", None)
    import memnos_mcp

    fake_time = _FakeTime()
    orig_time = memnos_mcp.time

    fired = {"count": 0}

    def fake_wait_idle_and_reexec():
        fired["count"] += 1

    orig_wait_idle = memnos_mcp._wait_idle_and_reexec

    # A (start, stable) -> A (unchanged) -> B (torn) -> C (torn again, still moving) ->
    # C (settled — should fire HERE, on the 5th call, not on the 3rd or 4th).
    scripted = iter(["A", "A", "B", "C", "C"])
    sig_calls = {"count": 0}

    def fake_version_signature():
        sig_calls["count"] += 1
        try:
            return next(scripted)
        except StopIteration:
            # loop kept polling after the script ran out — that alone is a failure
            # (should have fired on the 5th read); surface it plainly rather than
            # letting the debounce's own `except Exception: continue` swallow it.
            raise AssertionError(
                f"_reexec_watch_loop polled _version_signature() a 6th+ time "
                f"({sig_calls['count']} total) — should have fired after the "
                f"signature settled on the 4th/5th read") from None

    memnos_mcp.time = fake_time
    memnos_mcp._version_signature = fake_version_signature
    memnos_mcp._wait_idle_and_reexec = fake_wait_idle_and_reexec
    os.environ["MEMNOS_ADAPTER_REEXEC_INTERVAL_S"] = "1"

    try:
        print("=== driving _reexec_watch_loop with a scripted torn-then-settled sequence ===")
        memnos_mcp._reexec_watch_loop()

        check("fired exactly once", fired["count"] == 1, str(fired["count"]))
        check("did NOT act on the first observed change (B, still moving) — only "
              "3 signature reads (A start, A, B) had happened by the time a naive "
              "'act on any change' implementation would have already fired",
              sig_calls["count"] >= 4, str(sig_calls["count"]))
        check("settled on exactly the 5th read (C seen twice in a row: reads 4 and 5) "
              "— not the 3rd (B, only seen once) or the 4th (C, only seen once so far)",
              sig_calls["count"] == 5, str(sig_calls["count"]))
        check("used the debounce's extra settle tick — more than one sleep() happened "
              "after the initial change was first observed",
              fake_time.sleep_calls >= 4, str(fake_time.sleep_calls))

        print("=== control: a signature that never changes must never fire ===")
        fired["count"] = 0
        fake_time.sleep_calls = 0
        constant = iter(["Z"] * 10)
        memnos_mcp._version_signature = lambda: next(constant)
        # bound the control run itself (no natural termination otherwise)
        fake_time.MAX_SLEEPS = 8
        try:
            memnos_mcp._reexec_watch_loop()
            control_hit_cap = False
        except RuntimeError as e:
            control_hit_cap = "safety cap" in str(e)
        check("an unchanging signature never fires (loop only stops via the test's own "
              "safety cap, never via _wait_idle_and_reexec)",
              control_hit_cap and fired["count"] == 0,
              f"fired={fired['count']} hit_cap={control_hit_cap}")

    finally:
        memnos_mcp.time = orig_time
        memnos_mcp._wait_idle_and_reexec = orig_wait_idle
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
