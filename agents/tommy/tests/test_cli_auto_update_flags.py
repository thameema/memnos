"""`tommy --upgrade --auto-update`/`--no-auto-update`: the pure preference-toggle path
in cli.py's main(). No network, no real install — `update_check.set_auto_update` and
`_do_upgrade` are monkeypatched for the in-process CliRunner tests.

The "does the background check-on-launch block the real launch path" question is
answered out-of-process (subprocess + timeout), same pattern
test_auto_ingest_cli_wiring.py uses: an unknown HARNESS makes `_launch_harness` fail
fast right after the banner/update-check block, so a real launch runs end to end
without spawning anything, and a hang there would show up as this test timing out.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import tommy.cli as cli
import tommy.update_check as uc


def test_auto_update_flag_toggles_pref_and_does_not_run_upgrade(monkeypatch):
    calls = []
    monkeypatch.setattr(uc, "set_auto_update", lambda enabled: calls.append(enabled))
    monkeypatch.setattr(cli, "_do_upgrade", lambda: calls.append("upgraded!"))
    r = CliRunner().invoke(cli.main, ["--upgrade", "--auto-update"])
    assert r.exit_code == 0, r.output
    assert calls == [True]  # toggled, upgrade itself never called
    assert "enabled" in r.output


def test_no_auto_update_flag_toggles_pref_off(monkeypatch):
    calls = []
    monkeypatch.setattr(uc, "set_auto_update", lambda enabled: calls.append(enabled))
    r = CliRunner().invoke(cli.main, ["--upgrade", "--no-auto-update"])
    assert r.exit_code == 0, r.output
    assert calls == [False]
    assert "disabled" in r.output


def test_auto_update_flag_without_upgrade_is_rejected(monkeypatch):
    r = CliRunner().invoke(cli.main, ["--auto-update"])
    assert r.exit_code != 0
    assert "only make sense with --upgrade" in r.output


def test_plain_upgrade_without_auto_update_flag_still_calls_do_upgrade(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_do_upgrade", lambda: calls.append("upgraded!"))
    monkeypatch.setattr(uc, "set_auto_update", lambda enabled: calls.append(("toggled", enabled)))
    r = CliRunner().invoke(cli.main, ["--upgrade"])
    assert r.exit_code == 0, r.output
    assert calls == ["upgraded!"]  # no toggle call at all — set_auto_update is None


def _run(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from tommy.cli import main; main()", *args],
        capture_output=True, text=True, timeout=20, cwd=str(cwd), env=env,
    )


@pytest.fixture()
def isolated_env(tmp_path):
    env = dict(os.environ)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    return env


@pytest.fixture()
def unreachable_harness_conf(tmp_path):
    conf = tmp_path / "tommy.conf"
    conf.write_text("HARNESS=nonexistent_test_harness_xyz\n")
    return conf


@pytest.mark.parametrize("check_enabled", ["0", "1"])
def test_background_check_never_blocks_the_real_launch_path(
    check_enabled, tmp_path, isolated_env, unreachable_harness_conf,
):
    """TOMMY_UPDATE_CHECK=0: the check is skipped outright. TOMMY_UPDATE_CHECK=1 (the
    default): the check runs for real, possibly against a genuinely unreachable
    network in a sandboxed test runner — on a daemon thread, so the 20s subprocess
    timeout below is the actual proof it doesn't block: a regression that made this
    synchronous would hang here, not just run slow."""
    isolated_env["TOMMY_UPDATE_CHECK"] = check_enabled
    proc = _run(["--no-memnos-check", "--conf", str(unreachable_harness_conf)], tmp_path, isolated_env)
    assert "Unknown harness" in proc.stderr
    assert proc.returncode == 1
