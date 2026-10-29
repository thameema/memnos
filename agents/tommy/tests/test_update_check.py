"""`tommy.update_check`: check-on-launch PyPI check, print-once-per-version notice, and
the opt-in (default off) auto-update toggle. Mirrors memnos's own
`tests/test_update_check.py` for memnos_gateway.py, adapted to Tommy's check-on-launch
design (see update_check.py's module docstring for why it isn't a background loop).

No live network: `_latest_pypi_version` and `_spawn_tommy_upgrade` are monkeypatched
throughout. Each test points `CACHE_PATH` at its own tmp_path so tests never share state
or touch the real `~/.memnos/agents/tommy/`.
"""
from __future__ import annotations

import json

import tommy.update_check as uc


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "CACHE_PATH", tmp_path / "update_check.json")


def test_read_auto_update_pref_defaults_false_on_any_error(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    assert uc.read_auto_update_pref() is False  # no file at all
    (tmp_path / "update_check.json").write_text("{not valid json")
    assert uc.read_auto_update_pref() is False  # corrupt file -> False, not a crash


def test_set_auto_update_round_trips_and_preserves_other_fields(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    uc._write_state(latest_known="9.9.9", checked_at=123)
    uc.set_auto_update(True)
    state = json.loads((tmp_path / "update_check.json").read_text())
    assert state == {"latest_known": "9.9.9", "checked_at": 123, "auto_update": True}
    uc.set_auto_update(False)
    assert json.loads((tmp_path / "update_check.json").read_text())["auto_update"] is False


def test_no_notice_when_already_current(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: "1.0.0")
    spawn = []
    monkeypatch.setattr(uc, "_spawn_tommy_upgrade", lambda: spawn.append(1))
    uc.check_and_maybe_notify("1.0.0")
    assert capsys.readouterr().err == ""
    assert spawn == []
    assert json.loads((tmp_path / "update_check.json").read_text())["latest_known"] == "1.0.0"


def test_notice_printed_once_per_version_not_every_launch(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: "2.0.0")
    uc.check_and_maybe_notify("1.0.0")
    first = capsys.readouterr().err
    assert "UPDATE: v1.0.0 -> v2.0.0" in first
    # second "launch", same outdated current + same cached latest -> cache is still
    # fresh (checked_at just written) so this doesn't even hit the network again, and
    # the already-notified version must not print a second time.
    uc.check_and_maybe_notify("1.0.0")
    second = capsys.readouterr().err
    assert second == ""


def test_auto_update_off_by_default_notify_only(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: "2.0.0")
    spawn = []
    monkeypatch.setattr(uc, "_spawn_tommy_upgrade", lambda: spawn.append(1))
    uc.check_and_maybe_notify("1.0.0")
    assert spawn == []
    assert "UPDATE:" in capsys.readouterr().err


def test_auto_update_on_spawns_upgrade_as_separate_process(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    uc.set_auto_update(True)
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: "2.0.0")
    spawn = []
    monkeypatch.setattr(uc, "_spawn_tommy_upgrade", lambda: spawn.append(1))
    uc.check_and_maybe_notify("1.0.0")
    assert spawn == [1]
    assert "AUTO-UPDATE:" in capsys.readouterr().err


def test_fetch_failure_does_not_advance_checked_at_so_next_launch_retries(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: None)  # PyPI unreachable
    uc.check_and_maybe_notify("1.0.0")
    assert not (tmp_path / "update_check.json").exists()  # nothing cached -> nothing written


def test_fresh_cache_skips_network_entirely(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    uc._write_state(latest_known="1.0.0", checked_at=__import__("time").time())  # just checked
    called = []
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: called.append(1))
    uc.check_and_maybe_notify("1.0.0")
    assert called == []  # cache fresh -> no network call at all


def test_check_disabled_via_env_is_a_complete_noop(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "CHECK_ENABLED", False)
    called = []
    monkeypatch.setattr(uc, "_latest_pypi_version", lambda timeout=2.0: called.append(1))
    uc.check_and_maybe_notify("1.0.0")
    assert called == []
    assert not (tmp_path / "update_check.json").exists()


def test_never_raises_even_if_everything_inside_blows_up(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    def boom(timeout=2.0):
        raise RuntimeError("network stack is on fire")
    monkeypatch.setattr(uc, "_latest_pypi_version", boom)
    uc.check_and_maybe_notify("1.0.0")  # must not raise


def test_spawn_tommy_upgrade_launches_a_separate_process(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(uc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    uc._spawn_tommy_upgrade()
    assert len(calls) == 1
    args = calls[0][0][0]
    assert args[-1] == "--upgrade"
