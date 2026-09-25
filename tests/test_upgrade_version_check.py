"""issue #174: `memnos upgrade` must not report success purely from the upgrade
subprocess's exit code — `uv tool upgrade` (like `pip install -U`) exits 0 on a
genuine no-op ("Nothing to upgrade") too. Found live: a real install stayed on a much
older version after `memnos upgrade` printed "[memnos] ✓ upgraded to vX." — a following
`memnos --version` (and even a `memnos restart`) still showed the old version, because
the upgrade never actually happened; nothing in the output said so.

No network, no real `uv`/`pip`, no real subprocess — `cmd_upgrade`'s external
dependencies (_latest_pypi_version, _upgrade_cmd, subprocess.run, _installed_version,
_refresh_integrations, _server_up) are mocked so this runs the SAME function real users
hit, in-process, deterministically.

Run: python tests/test_upgrade_version_check.py
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memnos_cli as m

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def _run_upgrade(installed_sequence, subprocess_rc=0, args_check=False, args_no_restart=True):
    """installed_sequence: _installed_version()'s return value on each successive call —
    [0] is the pre-upgrade check, [1] is the post-subprocess re-check this issue adds."""
    calls = {"n": 0}
    def fake_installed_version():
        i = min(calls["n"], len(installed_sequence) - 1)
        calls["n"] += 1
        return installed_sequence[i]

    args = SimpleNamespace(check=args_check, no_restart=args_no_restart)
    cfg = {}
    exited = {}

    def fake_exit(msg=None):
        exited["called"] = True
        exited["msg"] = msg
        raise SystemExit(msg)

    with patch.object(m, "_installed_version", side_effect=fake_installed_version), \
         patch.object(m, "_latest_pypi_version", return_value="0.1.35"), \
         patch.object(m, "save_config", lambda c: None), \
         patch.object(m, "_upgrade_cmd", return_value=["fake-upgrader"]), \
         patch("subprocess.run") as mock_run, \
         patch.object(m, "_refresh_integrations", lambda: print("  refreshing ...")), \
         patch("sys.exit", side_effect=fake_exit):
        mock_run.return_value = SimpleNamespace(returncode=subprocess_rc)
        try:
            m.cmd_upgrade(args, cfg)
        except SystemExit:
            pass
    return exited


def main():
    print("=== issue #174: memnos upgrade verifies the version actually moved ===")

    # --- the real bug: uv/pip exits 0 but the version never actually changed ---
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exited = _run_upgrade(installed_sequence=["0.1.21", "0.1.21"], subprocess_rc=0)
    out = buf.getvalue()
    check("silent no-op: sys.exit is called (treated as a real failure, not success)",
          exited.get("called") is True, out)
    check("silent no-op: exit message says the version didn't move",
          exited.get("msg") and "still v0.1.21" in exited["msg"] and "v0.1.35" in exited["msg"],
          str(exited.get("msg")))
    check("silent no-op: exit message gives an actionable next step",
          exited.get("msg") and "uv cache clean" in exited["msg"], str(exited.get("msg")))
    check("silent no-op: the misleading '✓ upgraded' success line is NEVER printed",
          "✓ upgraded to v0.1.35" not in out, out)
    check("silent no-op: integrations are NOT refreshed after a failed upgrade",
          "refreshing ..." not in out, out)

    # --- the real success path: version genuinely changed ---
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exited = _run_upgrade(installed_sequence=["0.1.21", "0.1.35"], subprocess_rc=0)
    out = buf.getvalue()
    check("real upgrade: no failure exit", not exited.get("called"), out)
    check("real upgrade: success line prints the RE-CHECKED version, not just the target",
          "✓ upgraded to v0.1.35." in out, out)
    check("real upgrade: integrations ARE refreshed after a real upgrade",
          "refreshing ..." in out, out)

    # --- edge case: the re-check itself fails (e.g. metadata briefly unreadable
    # mid-swap) -> None, must be treated as failure, not crash ---
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exited = _run_upgrade(installed_sequence=["0.1.21", None], subprocess_rc=0)
    check("re-check returns None: treated as failure, not a crash",
          exited.get("called") is True and "v?" in (exited.get("msg") or ""),
          str(exited.get("msg")))

    # --- already on latest: no subprocess ever runs, unaffected by this fix ---
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exited = _run_upgrade(installed_sequence=["0.1.35"], subprocess_rc=0)
    out = buf.getvalue()
    check("already latest: no failure exit, no upgrade attempted",
          not exited.get("called") and "you're on the latest version" in out, out)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
