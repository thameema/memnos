"""
Schema tests for tommy.yaml (tommy/project_config.py) — issue #113.

Covers: valid parsing of every documented field, the version gate, the three
explicitly-excluded fields (platform / peer_approver / top-level harness)
being hard errors rather than silently ignored, unknown-key rejection, type
validation, and tommy.yaml discovery (walking up to — but never past — a
repo root).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tommy.project_config import (
    TommyYamlError,
    find_tommy_yaml,
    load_tommy_yaml,
    parse_tommy_yaml,
)

VALID_YAML = """
tommy:
  version: 1
project:
  name: Demo
  key: demo
  git_root: .
memnos:
  namespace: "org:demo:eng"
design_docs:
  - "docs/adr/*.md"
  - "docs/design/**/*.md"
corpus:
  corpus_gate: true
  auto_ingest: false
agents:
  default_model: claude-sonnet-4-5
  harness: claude
  smart_routing: true
  mcp_introspect: false
  skip_permissions: true
merge_gate: true
wave_limit: 4
"""


class TestValidParsing:
    def test_parses_every_documented_field(self):
        cfg = parse_tommy_yaml(VALID_YAML)
        assert cfg.version == 1
        assert cfg.project.name == "Demo"
        assert cfg.project.key == "demo"
        assert cfg.project.git_root == "."
        assert cfg.memnos.namespace == "org:demo:eng"
        assert cfg.design_docs == ["docs/adr/*.md", "docs/design/**/*.md"]
        assert cfg.corpus.corpus_gate is True
        assert cfg.corpus.auto_ingest is False
        assert cfg.agents.default_model == "claude-sonnet-4-5"
        assert cfg.agents.harness == "claude"
        assert cfg.agents.smart_routing is True
        assert cfg.agents.mcp_introspect is False
        assert cfg.agents.skip_permissions is True
        assert cfg.merge_gate is True
        assert cfg.wave_limit == 4

    def test_all_sections_optional_except_tommy_version(self):
        cfg = parse_tommy_yaml("tommy:\n  version: 1\n")
        assert cfg.project.name == ""
        assert cfg.project.key == ""
        assert cfg.project.git_root is None
        assert cfg.memnos.namespace == ""
        assert cfg.design_docs == []
        assert cfg.corpus.corpus_gate is None
        assert cfg.corpus.auto_ingest is None
        assert cfg.agents.default_model is None
        assert cfg.merge_gate is None
        assert cfg.wave_limit is None

    def test_load_from_disk_sets_source_path(self, tmp_path: Path):
        p = tmp_path / "tommy.yaml"
        p.write_text(VALID_YAML)
        cfg = load_tommy_yaml(p)
        assert cfg.source_path == p
        assert cfg.project.name == "Demo"


class TestVersionGate:
    def test_missing_tommy_version_is_an_error(self):
        with pytest.raises(TommyYamlError, match="tommy.version"):
            parse_tommy_yaml("project:\n  name: X\n")

    def test_missing_tommy_section_entirely_is_an_error(self):
        with pytest.raises(TommyYamlError, match="tommy.version"):
            parse_tommy_yaml("project:\n  name: X\n")

    def test_unsupported_version_is_an_error(self):
        with pytest.raises(TommyYamlError, match="not supported"):
            parse_tommy_yaml("tommy:\n  version: 99\n")

    def test_non_integer_version_is_an_error(self):
        with pytest.raises(TommyYamlError, match="integer"):
            parse_tommy_yaml('tommy:\n  version: "1"\n')


class TestExplicitExclusions:
    """Issue #113 explicitly excludes these — not omissions, hard errors."""

    def test_platform_field_is_rejected(self):
        with pytest.raises(TommyYamlError, match="platform"):
            parse_tommy_yaml("tommy:\n  version: 1\nplatform: github\n")

    def test_peer_approver_field_is_rejected(self):
        with pytest.raises(TommyYamlError, match="peer_approver"):
            parse_tommy_yaml("tommy:\n  version: 1\npeer_approver: someone\n")

    def test_top_level_harness_field_is_rejected(self):
        with pytest.raises(TommyYamlError, match="harness"):
            parse_tommy_yaml("tommy:\n  version: 1\nharness: claude\n")
        # But agents.harness (a suggestion, not a mandate) is fine.
        cfg = parse_tommy_yaml("tommy:\n  version: 1\nagents:\n  harness: claude\n")
        assert cfg.agents.harness == "claude"

    def test_scheduler_field_is_rejected(self):
        with pytest.raises(TommyYamlError, match="scheduler"):
            parse_tommy_yaml("tommy:\n  version: 1\nscheduler:\n  cron: '* * * * *'\n")


class TestUnknownAndMalformed:
    def test_unknown_top_level_key_is_rejected(self):
        with pytest.raises(TommyYamlError, match="unknown top-level"):
            parse_tommy_yaml("tommy:\n  version: 1\ntotally_made_up: true\n")

    def test_invalid_yaml_syntax_is_rejected(self):
        with pytest.raises(TommyYamlError, match="invalid YAML"):
            parse_tommy_yaml("tommy: [this is not\n  closed")

    def test_top_level_must_be_a_mapping(self):
        with pytest.raises(TommyYamlError, match="mapping"):
            parse_tommy_yaml("- just\n- a\n- list\n")

    def test_empty_file_is_missing_tommy_version(self):
        with pytest.raises(TommyYamlError, match="tommy.version"):
            parse_tommy_yaml("")


class TestTypeValidation:
    def test_wave_limit_must_be_int_not_bool(self):
        with pytest.raises(TommyYamlError, match="integer"):
            parse_tommy_yaml("tommy:\n  version: 1\nwave_limit: true\n")

    def test_wave_limit_must_be_int_not_string(self):
        with pytest.raises(TommyYamlError, match="integer"):
            parse_tommy_yaml('tommy:\n  version: 1\nwave_limit: "4"\n')

    def test_wave_limit_negative_is_rejected(self):
        with pytest.raises(TommyYamlError, match="wave_limit"):
            parse_tommy_yaml("tommy:\n  version: 1\nwave_limit: -1\n")

    def test_merge_gate_must_be_bool(self):
        with pytest.raises(TommyYamlError, match="true/false"):
            parse_tommy_yaml('tommy:\n  version: 1\nmerge_gate: "yes"\n')

    def test_corpus_gate_must_be_bool(self):
        with pytest.raises(TommyYamlError, match="true/false"):
            parse_tommy_yaml("tommy:\n  version: 1\ncorpus:\n  corpus_gate: 1\n")

    def test_design_docs_must_be_a_list(self):
        with pytest.raises(TommyYamlError, match="list"):
            parse_tommy_yaml("tommy:\n  version: 1\ndesign_docs: docs/*.md\n")

    def test_design_docs_entries_must_be_strings(self):
        with pytest.raises(TommyYamlError):
            parse_tommy_yaml("tommy:\n  version: 1\ndesign_docs:\n  - 5\n")

    def test_project_name_must_be_string(self):
        with pytest.raises(TommyYamlError, match="str"):
            parse_tommy_yaml("tommy:\n  version: 1\nproject:\n  name: 5\n")


class TestDiscovery:
    def test_finds_tommy_yaml_in_cwd(self, tmp_path: Path):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        found = find_tommy_yaml(tmp_path)
        assert found == tmp_path / "tommy.yaml"

    def test_walks_up_to_find_tommy_yaml(self, tmp_path: Path):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        found = find_tommy_yaml(nested)
        assert found == tmp_path / "tommy.yaml"

    def test_stops_at_git_root_without_walking_past_it(self, tmp_path: Path):
        # tommy.yaml lives ABOVE the .git root — must not be found, since the
        # search stops once it reaches (and checks) the repo root.
        (tmp_path / "tommy.yaml").write_text(VALID_YAML)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "src"
        nested.mkdir()
        found = find_tommy_yaml(nested)
        assert found is None

    def test_finds_tommy_yaml_exactly_at_git_root(self, tmp_path: Path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "tommy.yaml").write_text(VALID_YAML)
        nested = repo / "src" / "pkg"
        nested.mkdir(parents=True)
        found = find_tommy_yaml(nested)
        assert found == repo / "tommy.yaml"

    def test_returns_none_when_nothing_found(self, tmp_path: Path):
        nested = tmp_path / "x" / "y"
        nested.mkdir(parents=True)
        assert find_tommy_yaml(nested) is None
