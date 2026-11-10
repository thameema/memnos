"""Antigravity (agy) support in tommy, issue #232: harness registry, transcript discovery,
`tommy generate` adapter targets."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import tommy.cli as cli
from tommy.adapters import ADAPTER_TARGETS, BEGIN_MARKER, END_MARKER, generate_adapters
from tommy.discovery.harnesses import (
    HARNESS_REGISTRY,
    _SUPPORTS_SKIP_PERMISSIONS,
    apply_skip_permissions,
)
from tests.test_adapters import YAML_A, _effective


# ---- harness registry
@pytest.mark.parametrize("name", ["agy", "antigravity"])
def test_registry_has_agy(name):
    spec = HARNESS_REGISTRY[name]
    assert spec.binary == "agy" and spec.supports_tools and spec.supports_mcp
    assert spec.launch_template == ["agy", "--prompt-interactive", "{prompt_file}"]
    assert name in _SUPPORTS_SKIP_PERMISSIONS


@pytest.mark.parametrize("name", ["agy", "antigravity"])
def test_skip_permissions_applied_for_agy(name):
    cmd = apply_skip_permissions(["agy", "--prompt-interactive", "p"], name, True)
    assert cmd == ["agy", "--dangerously-skip-permissions", "--prompt-interactive", "p"]
    assert apply_skip_permissions(["agy", "x"], name, False) == ["agy", "x"]


def test_skip_permissions_still_not_applied_to_unsupported():
    assert apply_skip_permissions(["aider", "x"], "aider", True) == ["aider", "x"]


# ---- transcript discovery
def _transcript(home: Path, root: str, conv: str, mtime: float) -> Path:
    p = home / ".gemini" / root / "brain" / conv / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{}\n")
    os.utime(p, (mtime, mtime))
    return p


def test_find_latest_agy_transcript_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli._find_latest_agy_transcript() is None


def test_find_latest_agy_transcript_picks_newest_across_both_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    now = time.time()
    _transcript(tmp_path, "antigravity-cli", "a", now - 100)
    newest = _transcript(tmp_path, "antigravity", "b", now)
    _transcript(tmp_path, "antigravity-cli", "c", now - 50)
    assert cli._find_latest_agy_transcript() == newest


class _Client:
    def __init__(self):
        self.ingested = []

    def ingest_file(self, filename, text, extract):
        self.ingested.append(text)

    def consolidate(self):
        pass

    def remember(self, *a, **k):
        pass


@pytest.mark.parametrize("harness,expect_agy", [("agy", True), ("antigravity", True), ("claude", False)])
def test_post_run_capture_selects_finder_by_harness(monkeypatch, harness, expect_agy):
    monkeypatch.setattr(cli, "_find_latest_agy_transcript", lambda: _Fake("AGY"))
    monkeypatch.setattr(cli, "_find_latest_claude_transcript", lambda: _Fake("CLAUDE"))
    client = _Client()
    cfg = type("C", (), {"harness": harness, "tommy_ns": "x"})()
    cli._post_run_capture(client, cfg, "run1", None)
    assert client.ingested == ["AGY" if expect_agy else "CLAUDE"]


class _Fake:
    def __init__(self, text):
        self._t = text

    def read_text(self, errors=None):
        return self._t


# ---- tommy generate adapter targets
def test_adapter_targets_include_gemini_and_agents_rules():
    by = {t.name: t.rel_path for t in ADAPTER_TARGETS}
    assert by["gemini"] == "GEMINI.md" and by["antigravity"] == ".agents/rules/AGENTS.md"


def test_generate_updates_present_files_with_markers(tmp_path):
    (tmp_path / "GEMINI.md").write_text("# mine\n")
    (tmp_path / ".agents").mkdir()
    generate_adapters(_effective(tmp_path, YAML_A), tmp_path)
    g = (tmp_path / "GEMINI.md").read_text()
    assert g.startswith("# mine") and BEGIN_MARKER in g and END_MARKER in g
    # .agents/ exists, so the rules file is created even without --create-missing
    r = (tmp_path / ".agents" / "rules" / "AGENTS.md").read_text()
    assert BEGIN_MARKER in r and "Wave limit: 3" in r
    before = (g, r)
    generate_adapters(_effective(tmp_path, YAML_A), tmp_path)
    assert ((tmp_path / "GEMINI.md").read_text(), (tmp_path / ".agents/rules/AGENTS.md").read_text()) == before


def test_generate_skips_absent_and_create_missing_creates(tmp_path):
    eff = _effective(tmp_path, YAML_A)
    generate_adapters(eff, tmp_path)
    assert not (tmp_path / "GEMINI.md").exists() and not (tmp_path / ".agents").exists()
    generate_adapters(eff, tmp_path, create_missing=True)
    assert BEGIN_MARKER in (tmp_path / "GEMINI.md").read_text()
    assert (tmp_path / ".agents" / "rules" / "AGENTS.md").exists()


# ---- headless dispatch
def test_apply_headless_swaps_prompt_flag_for_agy_only(tmp_path):
    from tommy.discovery.harnesses import apply_headless
    tpl = ["agy", "--prompt-interactive", "nonexistent-literal-prompt"]
    for n in ("agy", "antigravity"):
        assert apply_headless(tpl, n) == ["agy", "-p", "nonexistent-literal-prompt"]
    pf = tmp_path / "prompt.md"
    pf.write_text("INLINE PROMPT BODY", encoding="utf-8")
    for n in ("agy", "antigravity"):
        assert apply_headless(["agy", "--prompt-interactive", str(pf)], n) == ["agy", "-p", "INLINE PROMPT BODY"]
    assert apply_headless(["claude", "--x"], "claude") == ["claude", "--x"]
