"""`memnos status` autostart-vs-pidfile reconciliation gate.

Bug: when the server runs via `memnos autostart` (launchd on macOS / systemd --user on
Linux) instead of `memnos start`, no pidfile is written — but status consulted ONLY the
pidfile and printed "STALE pid file" even though the server was healthy and responding. The
fix reconciles the pidfile check with ACTUAL liveness (healthz): if the server RESPONDS it is
RUNNING; the only genuine "stale pid file" warning is a DEAD pid AND nothing serving.

This gate drives the pure decision function `memnos_cli._background_status(running, svc,
pidstate)` across every branch — no real launchd/systemd service and no live server needed
(healthz + pidfile state are injected). $0, no OpenAI.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memnos_cli as cli

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def text(v):
    return "\n".join(v["lines"]).lower()


def main():
    print("=== memnos status: autostart vs pidfile ===")
    LAUNCHD = ("launchd", "/Users/x/Library/LaunchAgents/com.memnos.plist")
    SYSTEMD = ("systemd", "/home/x/.config/systemd/user/memnos.service")

    # 1. AUTOSTART-MANAGED, NO PIDFILE (the reported bug): healthz ok, autostart present,
    #    no pidfile → reports RUNNING (autostart-managed), NO stale warning.
    v = cli._background_status(running=True, svc=LAUNCHD, pidstate=("none", None))
    check("autostart + no pidfile → no STALE warning", v["stale_warning"] is False)
    check("autostart + no pidfile → reports running (autostart-managed)",
          v["managed"] == "autostart" and "autostart-managed" in text(v)
          and "stale" not in text(v))

    # 1b. systemd variant — same verdict, different kind label.
    v = cli._background_status(running=True, svc=SYSTEMD, pidstate=("none", None))
    check("systemd autostart + no pidfile → running, no stale warning",
          v["managed"] == "autostart" and v["stale_warning"] is False
          and "systemd" in text(v))

    # 2. AUTOSTART-MANAGED with a LEFTOVER dead pidfile (old `memnos start`): healthz ok,
    #    autostart present, pidfile pid dead → running (autostart-managed), stale pidfile
    #    is IGNORED, no scary STALE warning.
    v = cli._background_status(running=True, svc=LAUNCHD, pidstate=("dead", 4242))
    check("autostart + dead leftover pidfile → no STALE warning",
          v["stale_warning"] is False and v["managed"] == "autostart")
    check("autostart + dead leftover pidfile → notes it is ignored",
          "ignored" in text(v) and "autostart-managed" in text(v))

    # 3. GENUINELY DEAD server with a leftover pidfile: healthz DOWN, dead pid, no autostart
    #    → the STALE warning STILL fires (the case the original message was meant for).
    v = cli._background_status(running=False, svc=None, pidstate=("dead", 4242))
    check("dead server + leftover pidfile → STALE warning fires",
          v["stale_warning"] is True and "stale pid file" in text(v))

    # 4. NORMAL `memnos start`: pidfile pid alive → reports the pid, no stale warning.
    v = cli._background_status(running=True, svc=None, pidstate=("alive", 1234))
    check("alive pidfile (memnos start) → reports pid, no stale warning",
          v["managed"] == "start" and v["stale_warning"] is False
          and "pid 1234" in text(v) and "unmanaged" not in text(v))

    # 5. UNMANAGED live server (foreground/other manager, no pidfile, no autostart):
    #    running but neither start- nor autostart-managed → the ⚠ unmanaged hint, NOT stale.
    v = cli._background_status(running=True, svc=None, pidstate=("none", None))
    check("running + no pidfile + no autostart → unmanaged hint, not stale",
          v["stale_warning"] is False and v["managed"] == "none"
          and "unmanaged" in text(v))

    # 6. Server down, no pidfile at all → quiet (no background line, no warning).
    v = cli._background_status(running=False, svc=None, pidstate=("none", None))
    check("server down + no pidfile → no STALE warning, nothing to report",
          v["stale_warning"] is False and v["lines"] == [])

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
