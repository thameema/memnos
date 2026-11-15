"""Unit tests for core/wake_backends.py (Layer C adapter — memnos-wake).

Pure unit tests, no live orca/tmux binary or server needed: subprocess calls are mocked.
Run with pytest: python3 -m pytest tests/test_wake_backends.py -q
"""
import os
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wake_backends import (OrcaBackend, TmuxBackend, NoopBackend, select_backend,
                                 format_doorbell)


def test_format_doorbell_never_contains_raw_content_markers():
    text = format_doorbell("tommy-memnos", 123)
    assert "#123" in text and "@tommy-memnos" in text
    # the only inputs are a name and an id -- nothing resembling injected free text
    assert "content" not in text.lower()


def test_noop_backend_always_safe_fallback():
    b = NoopBackend()
    assert b.is_idle({"name": "x"}) is None
    assert b.send_doorbell({"name": "x"}, "hi") is False
    assert b.available() is True


def test_orca_backend_unavailable_without_binary():
    with patch("shutil.which", return_value=None):
        assert OrcaBackend.available() is False


def test_orca_backend_available_with_binary():
    with patch("shutil.which", return_value="/usr/local/bin/orca"):
        assert OrcaBackend.available() is True


def test_orca_backend_is_idle_true_when_terminal_waiting():
    b = OrcaBackend()
    fake = subprocess.CompletedProcess(args=[], returncode=0,
        stdout='[{"id": "term1", "state": "waiting"}]')
    with patch("subprocess.run", return_value=fake):
        assert b.is_idle({"orca_terminal_id": "term1"}) is True


def test_orca_backend_is_idle_none_without_metadata():
    b = OrcaBackend()
    assert b.is_idle({}) is None


def test_orca_backend_send_doorbell_false_on_subprocess_error():
    b = OrcaBackend()
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "orca")):
        assert b.send_doorbell({"orca_terminal_id": "term1"}, "hi") is False


def test_tmux_backend_unavailable_without_tmux_env_or_binary():
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    with patch.dict(os.environ, env, clear=True), patch("shutil.which", return_value=None):
        assert TmuxBackend.available() is False


def test_tmux_backend_available_with_tmux_env():
    with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1234,0"}):
        assert TmuxBackend.available() is True


def test_tmux_backend_is_idle_true_for_shell_pane():
    b = TmuxBackend()
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="%3 0 zsh\n%4 0 vim\n")
    with patch("subprocess.run", return_value=fake):
        assert b.is_idle({"tmux_pane": "%3"}) is True
        assert b.is_idle({"tmux_pane": "%4"}) is False


def test_select_backend_explicit_name():
    assert isinstance(select_backend("noop"), NoopBackend)


def test_select_backend_explicit_name_invalid():
    try:
        select_backend("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_select_backend_auto_falls_back_to_noop():
    with patch("shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
        assert isinstance(select_backend(), NoopBackend)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
