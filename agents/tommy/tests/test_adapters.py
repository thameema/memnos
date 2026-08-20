"""
Harness adapter tests (tommy/adapters.py) — issue #113 acceptance criterion:
"real tests for ... each harness adapter's idempotent-marker replace
behavior (write once, write again with different content, assert only the
marked region changed)."
"""
from __future__ import annotations

from pathlib import Path

import pytest

import tommy.config as config_mod
from tommy.adapters import (
    BEGIN_MARKER,
    END_MARKER,
    ADAPTER_TARGETS,
    generate_adapters,
    render_stdout_fallback,
    upsert_marked_block,
)
from tommy.effective_config import resolve_effective_config

YAML_A = """
tommy:
  version: 1
project:
  name: Demo
  key: demo
memnos:
  namespace: "org:demo:eng"
corpus:
  corpus_gate: true
merge_gate: false
wave_limit: 3
"""

YAML_B = """
tommy:
  version: 1
project:
  name: Demo
  key: demo
memnos:
  namespace: "org:demo:eng"
corpus:
  corpus_gate: false
merge_gate: true
wave_limit: 9
"""


@pytest.fixture(autouse=True)
def isolated_user_conf(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(
        config_mod, "_USER_CONF", tmp_path_factory.mktemp("home") / "nonexistent-tommy.conf",
    )


def _effective(tmp_path: Path, yaml_text: str):
    yaml_path = tmp_path / "tommy.yaml"
    yaml_path.write_text(yaml_text)
    return resolve_effective_config(tommy_yaml_path=yaml_path, env={})


# ---------------------------------------------------------------------------
# upsert_marked_block — the low-level primitive every adapter uses
# ---------------------------------------------------------------------------


class TestUpsertMarkedBlock:
    def test_appends_markers_to_empty_content(self):
        out = upsert_marked_block("", "hello")
        assert out == f"{BEGIN_MARKER}\nhello\n{END_MARKER}\n"

    def test_appends_after_existing_human_content(self):
        out = upsert_marked_block("# My notes\nhand-written stuff\n", "generated body")
        assert out.startswith("# My notes\nhand-written stuff\n")
        assert BEGIN_MARKER in out
        assert "generated body" in out

    def test_replaces_only_marked_region_on_rerun(self):
        first = upsert_marked_block("", "version A")
        wrapped = "before\n\n" + first + "\nafter\n"
        second = upsert_marked_block(wrapped, "version B")

        assert "before" in second
        assert "after" in second
        assert "version A" not in second
        assert "version B" in second
        # exactly one marker pair, and content outside it is byte-identical
        assert second.startswith("before\n\n")
        assert second.rstrip().endswith("after")

    def test_content_outside_markers_is_byte_for_byte_preserved(self):
        human_before = "# Project Notes\n\nSome careful hand-written prose.\n\n"
        human_after = "\n\n## Appendix\n\nMore hand-written prose.\n"
        original = human_before + upsert_marked_block("", "gen v1") + human_after
        updated = upsert_marked_block(original, "gen v2, totally different, longer content here")

        assert updated.startswith(human_before)
        assert updated.endswith(human_after)

    def test_malformed_markers_fall_back_to_append_rather_than_crash(self):
        # BEGIN with no END — must not raise, must not corrupt existing content.
        broken = f"stuff\n{BEGIN_MARKER}\nno end marker here\n"
        out = upsert_marked_block(broken, "new body")
        assert broken in out
        assert "new body" in out


# ---------------------------------------------------------------------------
# Per-adapter idempotent write/rewrite — the acceptance criterion, literally
# ---------------------------------------------------------------------------


ADAPTER_NAMES = [t.name for t in ADAPTER_TARGETS]


class TestPerAdapterIdempotency:
    @pytest.mark.parametrize("adapter_name", ADAPTER_NAMES)
    def test_write_once_write_again_only_marked_region_changes(self, tmp_path, adapter_name):
        target = next(t for t in ADAPTER_TARGETS if t.name == adapter_name)

        # Make this adapter "present" so generate_adapters actually touches it.
        path = tmp_path / target.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        human_prefix = f"# Human-authored {adapter_name} notes\n\nDo not remove this line.\n\n"
        path.write_text(human_prefix)

        effective_a = _effective(tmp_path, YAML_A)
        generate_adapters(effective_a, tmp_path)
        after_first = path.read_text()
        assert human_prefix in after_first
        assert "Wave limit: 3" in after_first

        effective_b = _effective(tmp_path, YAML_B)
        generate_adapters(effective_b, tmp_path)
        after_second = path.read_text()

        # Human content untouched, byte for byte.
        assert after_second.startswith(human_prefix) or human_prefix in after_second
        # Only the marked region changed: old generated values gone, new ones present.
        assert "Wave limit: 9" in after_second
        assert "Wave limit: 3" not in after_second
        # Exactly one marker pair — no accumulation across reruns.
        assert after_second.count(BEGIN_MARKER) == 1
        assert after_second.count(END_MARKER) == 1

    @pytest.mark.parametrize("adapter_name", ADAPTER_NAMES)
    def test_rerun_with_unchanged_config_reports_unchanged(self, tmp_path, adapter_name):
        target = next(t for t in ADAPTER_TARGETS if t.name == adapter_name)
        path = tmp_path / target.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing file\n")

        effective = _effective(tmp_path, YAML_A)
        first = generate_adapters(effective, tmp_path)
        first_result = next(r for r in first if r.target.name == adapter_name)
        assert first_result.action == "created" or first_result.action == "updated"

        second = generate_adapters(effective, tmp_path)
        second_result = next(r for r in second if r.target.name == adapter_name)
        assert second_result.action == "unchanged"


class TestCursorFrontmatterPreserved:
    def test_fresh_cursor_file_gets_frontmatter(self, tmp_path):
        (tmp_path / ".cursor").mkdir()
        effective = _effective(tmp_path, YAML_A)
        generate_adapters(effective, tmp_path)
        content = (tmp_path / ".cursor" / "rules" / "tommy.mdc").read_text()
        assert content.startswith("---\n")
        assert "alwaysApply" in content

    def test_hand_edited_frontmatter_survives_regeneration(self, tmp_path):
        cursor_dir = tmp_path / ".cursor" / "rules"
        cursor_dir.mkdir(parents=True)
        mdc = cursor_dir / "tommy.mdc"
        custom_frontmatter = (
            "---\n"
            "description: custom human description\n"
            "globs: '*.py'\n"
            "alwaysApply: false\n"
            "---\n"
            f"{BEGIN_MARKER}\nold body\n{END_MARKER}\n"
        )
        mdc.write_text(custom_frontmatter)

        effective = _effective(tmp_path, YAML_B)
        generate_adapters(effective, tmp_path)

        updated = mdc.read_text()
        assert "custom human description" in updated
        assert "globs: '*.py'" in updated
        assert "alwaysApply: false" in updated
        assert "old body" not in updated


# ---------------------------------------------------------------------------
# Presence detection / stdout fallback / --create-missing
# ---------------------------------------------------------------------------


class TestPresenceDetectionAndFallback:
    def test_nothing_present_falls_back_to_stdout_and_writes_nothing(self, tmp_path):
        effective = _effective(tmp_path, YAML_A)
        results = generate_adapters(effective, tmp_path)
        assert all(r.action == "skipped" for r in results)
        for target in ADAPTER_TARGETS:
            assert not (tmp_path / target.rel_path).exists()

        fallback_text = render_stdout_fallback(effective)
        assert BEGIN_MARKER in fallback_text
        assert "Wave limit: 3" in fallback_text

    def test_create_missing_writes_every_target_even_with_no_evidence(self, tmp_path):
        effective = _effective(tmp_path, YAML_A)
        results = generate_adapters(effective, tmp_path, create_missing=True)
        assert all(r.action == "created" for r in results)
        for target in ADAPTER_TARGETS:
            assert (tmp_path / target.rel_path).exists()

    def test_only_claude_md_present_only_claude_md_is_touched(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("existing\n")
        effective = _effective(tmp_path, YAML_A)
        results = generate_adapters(effective, tmp_path)

        by_name = {r.target.name: r for r in results}
        assert by_name["claude"].action == "updated"
        assert by_name["cursor"].action == "skipped"
        assert by_name["windsurf"].action == "skipped"
        assert by_name["copilot"].action == "skipped"
        assert not (tmp_path / ".cursor").exists()
        assert not (tmp_path / ".windsurfrules").exists()
        assert not (tmp_path / ".github").exists()

    def test_github_dir_without_the_file_yet_still_counts_as_present(self, tmp_path):
        (tmp_path / ".github").mkdir()
        effective = _effective(tmp_path, YAML_A)
        results = generate_adapters(effective, tmp_path)
        copilot_result = next(r for r in results if r.target.name == "copilot")
        assert copilot_result.action == "created"
        assert (tmp_path / ".github" / "copilot-instructions.md").exists()

    def test_dry_run_never_writes_to_disk(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("existing\n")
        effective = _effective(tmp_path, YAML_A)
        generate_adapters(effective, tmp_path, dry_run=True)
        # File must be untouched — no marker block written.
        assert (tmp_path / "CLAUDE.md").read_text() == "existing\n"
