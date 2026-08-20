"""
Precedence resolution tests (tommy/effective_config.py) — issue #113.

Precedence under test: tommy.conf (bundled default / user conf) -> tommy.yaml
(project, committed) -> env vars (highest). Every layer is exercised in
isolation and layered together, with provenance ("source") asserted at each
step so a regression that silently reorders precedence — or drops
provenance tracking — fails loudly.

Isolation: TommyConfig.load() reads a REAL, machine-specific
~/.memnos/agents/tommy/tommy.conf if one exists (it does, on a machine that
actually uses Tommy) — see test_dispatch_core_prompt_parity.py's own note
about TommyConfig.load()'s filesystem side effects for the same concern.
Every test here monkeypatches tommy.config._USER_CONF to a path that never
exists, so results only ever depend on the bundled tommy.conf.default
shipped in the package (deterministic) plus whatever this test explicitly
sets up.
"""
from __future__ import annotations

import pytest

import tommy.config as config_mod
from tommy.effective_config import (
    ENV_HARNESS,
    ENV_WAVE_LIMIT,
    resolve_effective_config,
)

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
  auto_ingest: false
agents:
  default_model: claude-opus-4-5
  harness: codex
  smart_routing: false
merge_gate: true
wave_limit: 7
"""


@pytest.fixture(autouse=True)
def isolated_user_conf(monkeypatch, tmp_path):
    """Every test in this file gets a guaranteed-nonexistent user tommy.conf,
    so results only depend on the bundled default + this test's own setup."""
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent-tommy.conf")


class TestConfLayerOnly:
    """No tommy.yaml anywhere — every field falls back to tommy.conf (bundled
    default) or, for fields with no tommy.conf equivalent, a built-in default."""

    def test_agent_fields_come_from_bundled_tommy_conf_default(self, tmp_path):
        effective = resolve_effective_config(project_root=tmp_path, env={})
        assert effective.value("harness") == "claude"
        assert effective.source("harness") == "tommy.conf"
        assert effective.value("default_model") == "claude-sonnet-4-5"
        assert effective.source("default_model") == "tommy.conf"
        assert effective.value("smart_routing") is True
        assert effective.source("smart_routing") == "tommy.conf"

    def test_yaml_only_fields_fall_back_to_builtin_defaults(self, tmp_path):
        effective = resolve_effective_config(project_root=tmp_path, env={})
        assert effective.value("merge_gate") is False
        assert effective.source("merge_gate") == "default"
        assert effective.value("wave_limit") == 4
        assert effective.source("wave_limit") == "default"
        assert effective.value("corpus_gate") is False
        assert effective.source("corpus_gate") == "default"
        assert effective.value("design_docs") == []
        assert effective.tommy_yaml_path is None

    def test_explicit_conf_path_overrides_bundled_default(self, tmp_path):
        conf = tmp_path / "custom.conf"
        conf.write_text("HARNESS=codex\nDEFAULT_MODEL=claude-opus-4-5\n")
        effective = resolve_effective_config(
            conf_path=conf, project_root=tmp_path, env={},
        )
        assert effective.value("harness") == "codex"
        assert effective.source("harness") == "tommy.conf"


class TestYamlLayerOverridesConf:
    def test_yaml_fields_override_conf_fields(self, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)
        effective = resolve_effective_config(tommy_yaml_path=yaml_path, env={})

        assert effective.value("harness") == "codex"
        assert effective.source("harness") == "tommy.yaml"
        assert effective.value("default_model") == "claude-opus-4-5"
        assert effective.value("smart_routing") is False
        assert effective.value("merge_gate") is True
        assert effective.value("wave_limit") == 7
        assert effective.value("corpus_gate") is True
        assert effective.value("auto_ingest") is False
        assert effective.value("namespace") == "org:demo:eng"
        assert effective.source("namespace") == "tommy.yaml"
        assert effective.value("design_docs") == ["docs/adr/*.md"]
        assert effective.value("project_name") == "Demo"
        assert effective.value("project_key") == "demo"

    def test_yaml_fields_left_unset_still_fall_back_to_conf(self, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text("tommy:\n  version: 1\nagents:\n  harness: codex\n")
        effective = resolve_effective_config(tommy_yaml_path=yaml_path, env={})

        # harness explicitly set in yaml -> overridden
        assert effective.value("harness") == "codex"
        assert effective.source("harness") == "tommy.yaml"
        # default_model NOT set in yaml -> still tommy.conf's bundled default
        assert effective.value("default_model") == "claude-sonnet-4-5"
        assert effective.source("default_model") == "tommy.conf"

    def test_yaml_discovery_by_walking_up_from_project_root(self, tmp_path):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        nested = tmp_path / "src"
        nested.mkdir()
        effective = resolve_effective_config(project_root=nested, env={})
        assert effective.tommy_yaml_path == tmp_path / "tommy.yaml"
        assert effective.value("harness") == "codex"

    def test_missing_tommy_yaml_is_not_an_error(self, tmp_path):
        effective = resolve_effective_config(project_root=tmp_path, env={})
        assert effective.tommy_yaml_path is None
        assert effective.value("harness") == "claude"  # falls back cleanly


class TestEnvLayerWinsOverEverything:
    def test_env_overrides_yaml_and_conf(self, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)  # sets harness=codex, wave_limit=7
        effective = resolve_effective_config(
            tommy_yaml_path=yaml_path,
            env={ENV_HARNESS: "aider", ENV_WAVE_LIMIT: "12"},
        )
        assert effective.value("harness") == "aider"
        assert effective.source("harness") == "env"
        assert effective.value("wave_limit") == 12
        assert effective.source("wave_limit") == "env"
        # untouched fields still come from yaml
        assert effective.value("default_model") == "claude-opus-4-5"
        assert effective.source("default_model") == "tommy.yaml"

    def test_env_overrides_conf_even_with_no_yaml_present(self, tmp_path):
        effective = resolve_effective_config(
            project_root=tmp_path, env={ENV_HARNESS: "goose"},
        )
        assert effective.value("harness") == "goose"
        assert effective.source("harness") == "env"

    def test_env_bool_coercion(self, tmp_path):
        from tommy.effective_config import ENV_MERGE_GATE

        for raw, expected in [("on", True), ("true", True), ("1", True),
                               ("off", False), ("false", False), ("0", False)]:
            effective = resolve_effective_config(
                project_root=tmp_path, env={ENV_MERGE_GATE: raw},
            )
            assert effective.value("merge_gate") is expected, raw


class TestProvenanceCompleteness:
    def test_every_documented_field_has_a_source(self, tmp_path):
        yaml_path = tmp_path / "tommy.yaml"
        yaml_path.write_text(VALID_YAML)
        effective = resolve_effective_config(tommy_yaml_path=yaml_path, env={})
        expected_fields = {
            "project_name", "project_key", "project_git_root",
            "namespace", "design_docs",
            "corpus_gate", "auto_ingest",
            "default_model", "harness", "smart_routing", "mcp_introspect",
            "skip_permissions", "merge_gate", "wave_limit",
        }
        assert expected_fields <= set(effective.fields.keys())
        for name in expected_fields:
            assert effective.source(name) in ("tommy.conf", "tommy.yaml", "env", "default")

    def test_as_dict_and_provenance_dict_agree(self, tmp_path):
        effective = resolve_effective_config(project_root=tmp_path, env={})
        d = effective.as_dict()
        p = effective.as_provenance_dict()
        assert set(d.keys()) == set(p.keys())
        for name, value in d.items():
            assert p[name]["value"] == value
