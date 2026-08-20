"""
cli.py's main() dispatch to `generate` / `config` — issue #113.

main() stays a single click.Command (context_settings ignore_unknown_options +
allow_extra_args + a UNPROCESSED extra_args catch-all) rather than becoming a
click.Group, to preserve `tommy [FLAGS] [-- harness args]` — see the comment
above the dispatch in cli.py. These tests prove that dispatch actually fires
(and short-circuits before the memnos-check / harness-launch machinery) for
both subcommands, and that plain `tommy` invocations — including `--help` —
are unaffected.

Out-of-process (subprocess + timeout), same pattern test_version.py already
uses for `--version`: the regression this guards against is exactly the kind
that only shows up as a real launch attempt (banner, memnos health check,
harness spawn) hanging or blocking instead of the subcommand short-circuiting
cleanly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

VALID_YAML = """
tommy:
  version: 1
project:
  name: Demo
  key: demo
memnos:
  namespace: "org:demo:eng"
agents:
  harness: codex
wave_limit: 5
"""


def _run(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from tommy.cli import main; main()", *args],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(cwd),
        env=env,
    )


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    import os

    env = dict(os.environ)
    # Guaranteed-nonexistent HOME so ~/.memnos/agents/tommy/tommy.conf (a real,
    # machine-specific file) never leaks into these assertions.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env.pop("OPENAI_API_KEY", None)
    return env


class TestGenerateDispatch:
    def test_tommy_generate_help_shows_subcommand_help_not_top_level(self, tmp_path, isolated_env):
        proc = _run(["generate", "--help"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "tommy generate" in proc.stdout
        assert "--dry-run" in proc.stdout
        # Must NOT be the top-level tommy help (no --list-harnesses etc.)
        assert "--list-harnesses" not in proc.stdout

    def test_tommy_generate_runs_without_touching_memnos_or_launching_harness(self, tmp_path, isolated_env):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        (tmp_path / "CLAUDE.md").write_text("hello\n")
        proc = _run(["generate"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "memnos-native coding orchestrator" not in proc.stdout
        assert "memnos-native coding orchestrator" not in proc.stderr
        claude_md = (tmp_path / "CLAUDE.md").read_text()
        assert "Wave limit: 5" in claude_md


class TestConfigDispatch:
    def test_tommy_config_show_help(self, tmp_path, isolated_env):
        proc = _run(["config", "show", "--help"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "tommy config show" in proc.stdout

    def test_tommy_config_show_runs_without_launching_harness(self, tmp_path, isolated_env):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        proc = _run(["config", "show"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "codex" in proc.stdout
        assert "memnos-native coding orchestrator" not in proc.stdout


class TestUnaffectedBaselineBehavior:
    def test_tommy_help_still_works(self, tmp_path, isolated_env):
        proc = _run(["--help"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "Tommy" in proc.stdout
        assert "--list-harnesses" in proc.stdout

    def test_tommy_list_harnesses_unaffected(self, tmp_path, isolated_env):
        proc = _run(["--list-harnesses"], tmp_path, isolated_env)
        assert proc.returncode == 0, proc.stderr
        assert "Harnesses:" in proc.stdout
