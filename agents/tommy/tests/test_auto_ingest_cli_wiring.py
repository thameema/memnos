"""
Call-site wiring test for cli.py's auto-ingest (issue #109) — proves
`tommy` (the real launch path, not `tommy generate`/`tommy config show`)
actually reaches `_auto_ingest_changed_docs()` when tommy.yaml's
corpus.auto_ingest is true, and does NOT when it's false/absent.

Out-of-process (subprocess + timeout), same pattern test_cli_dispatch.py
and test_version.py use. `--no-memnos-check` keeps this test from touching
a real memnos server (memnos_client is None, so _auto_ingest_changed_docs
logs "memnos unreachable" and returns immediately — see test_auto_ingest.py
for the function's own behavior when memnos IS reachable). HARNESS is
pointed at a name no HarnessSpec will ever match, so _launch_harness exits
fast (code 1, "Unknown harness") right after the auto-ingest step runs,
without spawning a real subprocess — keeping this test fast and hermetic
while still exercising the real main() call path end to end.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE_YAML = """
tommy:
  version: 1
design_docs:
  - "docs/**/*.md"
corpus:
  auto_ingest: {flag}
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
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env.pop("OPENAI_API_KEY", None)
    return env


@pytest.fixture()
def unreachable_harness_conf(tmp_path):
    conf = tmp_path / "tommy.conf"
    conf.write_text("HARNESS=nonexistent_test_harness_xyz\n")
    return conf


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo), check=True)


class TestAutoIngestCallSite:
    def test_auto_ingest_true_reaches_the_auto_ingest_step(
        self, tmp_path, isolated_env, unreachable_harness_conf,
    ):
        _init_git_repo(tmp_path)
        (tmp_path / "tommy.yaml").write_text(GATE_YAML.format(flag="true"))

        proc = _run(
            ["--no-memnos-check", "--conf", str(unreachable_harness_conf)],
            tmp_path, isolated_env,
        )

        # Reached the auto-ingest step (memnos unreachable under --no-memnos-check,
        # so it no-ops there) and THEN failed fast at the harness step, proving
        # the auto-ingest call site runs before harness launch without blocking it.
        assert "auto_ingest: skipped (memnos unreachable)" in proc.stderr
        assert "Unknown harness" in proc.stderr
        assert proc.returncode == 1

    def test_auto_ingest_false_never_reaches_the_auto_ingest_step(
        self, tmp_path, isolated_env, unreachable_harness_conf,
    ):
        _init_git_repo(tmp_path)
        (tmp_path / "tommy.yaml").write_text(GATE_YAML.format(flag="false"))

        proc = _run(
            ["--no-memnos-check", "--conf", str(unreachable_harness_conf)],
            tmp_path, isolated_env,
        )

        assert "auto_ingest" not in proc.stderr
        assert "Unknown harness" in proc.stderr
        assert proc.returncode == 1

    def test_no_tommy_yaml_defaults_to_auto_ingest_off(
        self, tmp_path, isolated_env, unreachable_harness_conf,
    ):
        _init_git_repo(tmp_path)
        # No tommy.yaml at all.
        proc = _run(
            ["--no-memnos-check", "--conf", str(unreachable_harness_conf)],
            tmp_path, isolated_env,
        )
        assert "auto_ingest" not in proc.stderr
        assert "Unknown harness" in proc.stderr
        assert proc.returncode == 1
