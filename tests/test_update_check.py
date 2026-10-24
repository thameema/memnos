"""Update notify / opt-in auto-update: `memnos_gateway.py`'s background
`_update_check_loop` (detect + cache + log, optional auto-install via a SEPARATE
subprocess) and `memnos_cli.py`'s cache-read / hint-surfacing side.

No live server or database needed — this is pure function logic plus file I/O against
an isolated temp HOME, and one real `memnos upgrade --auto-update`/`--no-auto-update`
subprocess invocation (a config write only; no network, no install attempted).

Run: python tests/test_update_check.py
"""
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def _isolated_gateway(tmpdir):
    """Import (or reuse) memnos_gateway and point its CONFIG_DIR / UPDATE_CACHE_PATH at
    an isolated tempdir — these are plain module globals computed once at import time
    from the real $HOME, so a test has to reassign them directly rather than relying on
    a HOME env var the module already read."""
    import memnos_gateway as gw
    gw.CONFIG_DIR = tmpdir
    gw.UPDATE_CACHE_PATH = os.path.join(tmpdir, "update_check.json")
    return gw


def test_write_and_read_update_cache():
    print("=== _write_update_cache round-trips, atomic (temp + replace) ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        gw._write_update_cache("0.9.9")
        check("cache file exists", os.path.exists(gw.UPDATE_CACHE_PATH))
        check("no leftover .tmp* file", not any(f.startswith("update_check.json.tmp") for f in os.listdir(tmp)))
        with open(gw.UPDATE_CACHE_PATH) as f:
            data = json.load(f)
        check("latest_known written", data.get("latest_known") == "0.9.9")
        check("last_checked_at written", isinstance(data.get("last_checked_at"), (int, float)))


def test_update_check_tick_no_update_when_current():
    print("=== _update_check_tick: no UPDATE log, no auto-update attempt when already current ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        with mock.patch.object(gw, "_latest_pypi_version", return_value="1.0.0"), \
             mock.patch.object(gw, "_spawn_auto_update") as spawn:
            latest = gw._update_check_tick("1.0.0")
        check("returns the latest version", latest == "1.0.0")
        check("cache still written even when current", os.path.exists(gw.UPDATE_CACHE_PATH))
        check("auto-update never attempted when not outdated", spawn.call_count == 0)


def test_update_check_tick_dedupes_the_log_line_across_ticks():
    print("=== _update_check_tick: logs 'UPDATE available' once, not every tick, for the same version ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        with mock.patch.object(gw, "_latest_pypi_version", return_value="2.0.0"):
            with mock.patch("builtins.print") as p1:
                gw._update_check_tick("1.0.0")
            first_tick_logged = any("UPDATE:" in str(c) for c in p1.call_args_list)
            with mock.patch("builtins.print") as p2:
                gw._update_check_tick("1.0.0")   # same still-outdated "current", same "latest"
            second_tick_logged = any("UPDATE:" in str(c) for c in p2.call_args_list)
        check("first tick logs the UPDATE line", first_tick_logged)
        check("second tick (same latest already cached) does NOT re-log it", not second_tick_logged)
        check("cache is still written on the repeat tick (notify-hint freshness unaffected)",
              os.path.exists(gw.UPDATE_CACHE_PATH))


def test_update_check_tick_notify_only_by_default():
    print("=== _update_check_tick: outdated + auto_update OFF (default) -> notify, no spawn ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        # no config.json at all in this tmp dir -> _read_auto_update_pref() must default False
        with mock.patch.object(gw, "_latest_pypi_version", return_value="2.0.0"), \
             mock.patch.object(gw, "_spawn_auto_update") as spawn:
            latest = gw._update_check_tick("1.0.0")
        check("returns the latest version", latest == "2.0.0")
        check("auto-update NOT attempted with no config / default off", spawn.call_count == 0)


def test_update_check_tick_auto_update_on_spawns_subprocess():
    print("=== _update_check_tick: outdated + auto_update ON -> spawns the upgrade subprocess ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump({"auto_update": True}, f)
        with mock.patch.object(gw, "_latest_pypi_version", return_value="2.0.0"), \
             mock.patch.object(gw, "_spawn_auto_update") as spawn:
            gw._update_check_tick("1.0.0")
        check("auto-update attempted exactly once", spawn.call_count == 1)


def test_spawn_auto_update_never_runs_installer_inline():
    print("=== _spawn_auto_update: launches a SEPARATE process, never runs the installer inline ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        with mock.patch.object(gw.subprocess, "Popen") as popen:
            gw._spawn_auto_update()
        check("Popen called (a new process, not a direct installer call in this process)",
              popen.call_count == 1)
        args = popen.call_args[0][0]
        check("invokes 'upgrade --no-restart', not a bare installer command",
              args[-2:] == ["upgrade", "--no-restart"], args)


def test_read_auto_update_pref_defaults_false_on_any_error():
    print("=== _read_auto_update_pref: missing/corrupt config.json -> False, never True by accident ===")
    with tempfile.TemporaryDirectory() as tmp:
        gw = _isolated_gateway(tmp)
        check("missing config.json -> False", gw._read_auto_update_pref() is False)
        with open(os.path.join(tmp, "config.json"), "w") as f:
            f.write("{not valid json")
        check("corrupt config.json -> False (not a crash)", gw._read_auto_update_pref() is False)
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump({"auto_update": True}, f)
        check("valid config.json with auto_update:true -> True", gw._read_auto_update_pref() is True)


def test_cli_latest_known_prefers_cache_then_falls_back_to_config():
    print("=== memnos_cli._latest_known: cache file first, cfg['latest_known'] fallback ===")
    import memnos_cli as cli
    orig_path = cli.UPDATE_CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cli.UPDATE_CACHE_PATH = os.path.join(tmp, "update_check.json")
            check("neither source -> None", cli._latest_known({}) is None)
            check("config-only fallback still works (pre-existing manual `upgrade --check` behavior)",
                  cli._latest_known({"latest_known": "3.0.0"}) == "3.0.0")
            with open(cli.UPDATE_CACHE_PATH, "w") as f:
                json.dump({"latest_known": "4.0.0"}, f)
            check("cache file wins over a stale config value",
                  cli._latest_known({"latest_known": "3.0.0"}) == "4.0.0")
    finally:
        cli.UPDATE_CACHE_PATH = orig_path


def test_cli_auto_update_toggle_subprocess():
    print("=== `memnos upgrade --auto-update` / `--no-auto-update`: sets config, no network ===")
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp, MEMNOS_CI="1")
        r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "upgrade", "--auto-update"],
                           capture_output=True, text=True, env=env, timeout=15)
        check("--auto-update exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("--auto-update says enabled", "enabled" in (r.stdout + r.stderr))
        cfg_path = os.path.join(tmp, ".memnos", "config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        check("config.json: auto_update is true", cfg.get("auto_update") is True)

        r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "upgrade", "--no-auto-update"],
                           capture_output=True, text=True, env=env, timeout=15)
        check("--no-auto-update exits 0", r.returncode == 0, r.stdout + r.stderr)
        with open(cfg_path) as f:
            cfg = json.load(f)
        check("config.json: auto_update is false after --no-auto-update", cfg.get("auto_update") is False)

        r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "upgrade",
                            "--auto-update", "--no-auto-update"],
                           capture_output=True, text=True, env=env, timeout=15)
        check("--auto-update and --no-auto-update together is rejected (mutually exclusive)",
              r.returncode != 0)


def main():
    test_write_and_read_update_cache()
    test_update_check_tick_no_update_when_current()
    test_update_check_tick_dedupes_the_log_line_across_ticks()
    test_update_check_tick_notify_only_by_default()
    test_update_check_tick_auto_update_on_spawns_subprocess()
    test_spawn_auto_update_never_runs_installer_inline()
    test_read_auto_update_pref_defaults_false_on_any_error()
    test_cli_latest_known_prefers_cache_then_falls_back_to_config()
    test_cli_auto_update_toggle_subprocess()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
