"""Tommy's `_do_upgrade()` had the exact bug memnos's `cmd_upgrade` fixed in issue #175:
a zero exit code from `uv tool upgrade`/`pip install -U` is not proof the package
actually changed (a stale package-index cache can make the installer exit 0 having done
nothing). `_do_upgrade()` now re-reads the installed version after the subprocess runs
and fails loudly if it didn't move — this file is the regression test for that fix.

No real install, no network: `subprocess.run` (the installer call) and
`_tommy_installed_version` (the before/after check) are both monkeypatched.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

import tommy.cli as cli


def _fake_completed(returncode):
    r = MagicMock()
    r.returncode = returncode
    return r


def test_upgrade_reports_failure_when_installer_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_installer", lambda pkg: "pip")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(1))
    monkeypatch.setattr(cli, "_tommy_installed_version", lambda: "0.1.7")
    with pytest.raises(SystemExit) as exc:
        cli._do_upgrade()
    assert exc.value.code == 1
    assert "Upgrade failed" in capsys.readouterr().err


def test_upgrade_catches_a_silent_noop_despite_exit_zero(monkeypatch, capsys):
    """The actual bug: installer exits 0, but the version never moved (stale index
    cache). Must fail loudly, not report success."""
    monkeypatch.setattr(cli, "_detect_installer", lambda pkg: "pip")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0))
    versions = iter(["0.1.7", "0.1.7"])   # before == after: nothing actually changed
    monkeypatch.setattr(cli, "_tommy_installed_version", lambda: next(versions))
    with pytest.raises(SystemExit) as exc:
        cli._do_upgrade()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "silently did nothing" in err
    assert "stale package-index cache" in err


def test_upgrade_reports_success_when_version_actually_moved(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_installer", lambda pkg: "pip")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0))
    versions = iter(["0.1.7", "0.1.8"])   # before != after: a real upgrade happened
    monkeypatch.setattr(cli, "_tommy_installed_version", lambda: next(versions))
    cli._do_upgrade()   # must NOT raise/exit nonzero
    out = capsys.readouterr().out
    assert "Tommy upgraded" in out
    assert "v0.1.8" in out


def test_upgrade_does_not_false_positive_when_version_unreadable(monkeypatch, capsys):
    """`_tommy_installed_version()` returning None (e.g. a dev checkout with no
    installed distribution) must not be misread as "before == after" and trigger a
    false silent-noop failure — there's genuinely nothing to compare."""
    monkeypatch.setattr(cli, "_detect_installer", lambda pkg: "pip")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0))
    monkeypatch.setattr(cli, "_tommy_installed_version", lambda: None)
    cli._do_upgrade()   # must NOT raise/exit nonzero
    assert "Tommy upgraded" in capsys.readouterr().out
