"""
Stand-in "harness" process for test_launch_harness_sigint.py.

Launched by the REAL _launch_harness() (via a fake HarnessSpec) exactly the
way a real harness binary (claude, codex, ...) would be. Two modes, selected
by TOMMY_TEST_HARNESS_MODE:

  "sleep" (default) — writes its own PID to TOMMY_TEST_HARNESS_PIDFILE, installs
      a SIGINT handler that records "sigint-received" in TOMMY_TEST_HARNESS_MARKERFILE
      and exits, then sleeps for TOMMY_TEST_HARNESS_SLEEP seconds. If the sleep
      completes without SIGINT ever arriving, it appends "timeout-no-sigint" to
      the marker file instead — this is what happens under the pre-#77 bug,
      where the harness lands in a different process group and never sees Ctrl-C.

  "exit" — writes its PID, then exits immediately with TOMMY_TEST_HARNESS_EXITCODE.
      Used to exercise the normal (non-interrupted) exit path.
"""
import os
import signal
import sys
import time

pid_file = os.environ["TOMMY_TEST_HARNESS_PIDFILE"]
marker_file = os.environ["TOMMY_TEST_HARNESS_MARKERFILE"]
mode = os.environ.get("TOMMY_TEST_HARNESS_MODE", "sleep")

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
    f.flush()

if mode == "exit":
    sys.exit(int(os.environ.get("TOMMY_TEST_HARNESS_EXITCODE", "0")))


def _on_sigint(signum, frame):
    with open(marker_file, "w") as mf:
        mf.write("sigint-received")
        mf.flush()
    sys.exit(0)


signal.signal(signal.SIGINT, _on_sigint)

time.sleep(float(os.environ.get("TOMMY_TEST_HARNESS_SLEEP", "30")))

# Only reached if SIGINT never arrived within the sleep window.
with open(marker_file, "a") as mf:
    mf.write("timeout-no-sigint")
sys.exit(0)
