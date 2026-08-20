"""
`tommy config show` output tests (tommy/generate_cmd.py) — issue #113
acceptance criterion: "real tests for ... `tommy config show` output."

Uses click.testing.CliRunner directly against config_group/generate_command
rather than spawning a subprocess — faster, and the object under test is
exactly the click.Command dispatched from cli.py's main() (see cli.py's
comment on why `main` stays a single click.Command and dispatches to these
by hand rather than being a click.Group itself).
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import tommy.config as config_mod
from tommy.generate_cmd import config_group, generate_command

VALID_YAML = """
tommy:
  version: 1
project:
  name: Demo
  key: demo
memnos:
  namespace: "org:demo:eng"
design_docs:
  - "docs/adr/*.md"
corpus:
  corpus_gate: true
  auto_ingest: true
agents:
  default_model: claude-opus-4-5
  harness: codex
merge_gate: true
wave_limit: 6
"""


@pytest.fixture(autouse=True)
def isolated_user_conf(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(
        config_mod, "_USER_CONF", tmp_path_factory.mktemp("home") / "nonexistent-tommy.conf",
    )


@pytest.fixture()
def runner():
    return CliRunner()


class TestConfigShowText:
    def test_shows_resolved_values_and_sources(self, runner, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)

        result = runner.invoke(config_group, ["show", "--tommy-yaml", str(yaml_path)])
        assert result.exit_code == 0, result.output
        assert "codex" in result.output
        assert "[tommy.yaml]" in result.output
        assert "claude-opus-4-5" in result.output
        assert str(yaml_path) in result.output

    def test_no_tommy_yaml_still_shows_defaults(self, runner, tmp_path):
        result = runner.invoke(config_group, ["show", "--root", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "(none found)" in result.output
        assert "claude" in result.output  # bundled tommy.conf default harness
        assert "[tommy.conf]" in result.output

    def test_invalid_tommy_yaml_exits_nonzero_with_clear_error(self, runner, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("tommy:\n  version: 1\nplatform: github\n")
        result = runner.invoke(config_group, ["show", "--tommy-yaml", str(bad)])
        assert result.exit_code != 0
        assert "platform" in result.output


class TestConfigShowJson:
    def test_json_output_is_parseable_and_matches_text_values(self, runner, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)

        result = runner.invoke(
            config_group, ["show", "--tommy-yaml", str(yaml_path), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)

        assert payload["tommy_yaml_path"] == str(yaml_path)
        fields = payload["fields"]
        assert fields["harness"] == {"value": "codex", "source": "tommy.yaml"}
        assert fields["default_model"] == {"value": "claude-opus-4-5", "source": "tommy.yaml"}
        assert fields["merge_gate"] == {"value": True, "source": "tommy.yaml"}
        assert fields["wave_limit"] == {"value": 6, "source": "tommy.yaml"}
        assert fields["corpus_gate"] == {"value": True, "source": "tommy.yaml"}
        assert fields["auto_ingest"] == {"value": True, "source": "tommy.yaml"}
        assert fields["namespace"] == {"value": "org:demo:eng", "source": "tommy.yaml"}
        assert fields["design_docs"] == {"value": ["docs/adr/*.md"], "source": "tommy.yaml"}
        # not set in yaml -> falls back to tommy.conf's bundled default
        assert fields["smart_routing"]["source"] == "tommy.conf"

    def test_json_output_reflects_env_override(self, runner, tmp_path, monkeypatch):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)
        monkeypatch.setenv("TOMMY_CFG_HARNESS", "aider")

        result = runner.invoke(
            config_group, ["show", "--tommy-yaml", str(yaml_path), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["fields"]["harness"] == {"value": "aider", "source": "env"}


class TestGenerateCommandCli:
    def test_generate_writes_present_adapters(self, runner, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("existing\n")
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)

        result = runner.invoke(
            generate_command, ["--tommy-yaml", str(yaml_path), "--root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        claude_md = (tmp_path / "CLAUDE.md").read_text()
        assert "Wave limit: 6" in claude_md
        assert "existing" in claude_md

    def test_generate_dry_run_writes_nothing(self, runner, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("existing\n")
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)

        result = runner.invoke(
            generate_command,
            ["--tommy-yaml", str(yaml_path), "--root", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "CLAUDE.md").read_text() == "existing\n"
        assert "dry-run" in result.output

    def test_generate_with_invalid_yaml_exits_nonzero(self, runner, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("tommy:\n  version: 1\npeer_approver: bob\n")
        result = runner.invoke(generate_command, ["--tommy-yaml", str(bad), "--root", str(tmp_path)])
        assert result.exit_code != 0
        assert "peer_approver" in result.output
