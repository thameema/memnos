"""
Auto-ingest tests — issue #109 (cli.py's _auto_ingest_changed_docs()).

Covers:
  - _git_diff_base(): HEAD~10 preferred, falling back to the oldest reachable
    commit (root / shallow boundary) on short histories, None outside a git
    repo — never a crash.
  - _changed_files_since(): real git diff --name-only over a real repo.
  - _auto_ingest_changed_docs(): the full flow — glob matching, ingesting a
    REAL design-doc fixture with genuine RFC-2119 (SHALL/MUST/SHOULD)
    language, the "silent constraint wipe" warning, and every no-op/failure
    path (memnos unreachable, non-git dir, non-matching glob, deleted file,
    read-only-token 403) staying non-fatal.

All memnos calls are made through tommy.corpus's corpus_ingest/corpus_list
(monkeypatched here as cli_mod._http_corpus_ingest / _http_corpus_list) —
this suite proves Tommy's own responsibility (which real file content gets
sent, under what name/namespace/git_sha, and how the response is surfaced),
not the server's extraction correctness, which tests/test_corpus_api.py
already covers end-to-end against a real Postgres, and which
tests/test_corpus_gate_tommy.py (root suite) covers through this exact
fixture over a real HTTP round trip. The mock ingest handler below
independently re-applies the SAME RFC-2119 keyword regex core/store.py's
real extractor uses (core/store.py:1760-1761) against whatever text it
receives, so a passing test here is also proof the fixture content is
genuinely SHALL/MUST-extractable, not just an arbitrary string.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import tommy.cli as cli_mod
import tommy.config as config_mod
from tommy.effective_config import resolve_effective_config

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_ADR = FIXTURES / "sample_adr.md"

# Mirrors core/store.py:1760-1761 exactly — used ONLY here, to let the mock
# /corpus/ingest handler independently verify (and report a realistic
# constraint count for) whatever text Tommy actually sends, without
# depending on a live server. Not imported from core.store: that package
# isn't a dependency of tommy-orchestrator and pulls in psycopg et al.
_CONSTRAINT_RE = re.compile(
    r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|SHALL|MUST|REQUIRED|SHOULD|PROHIBITED|FORBIDDEN)\b")


def _count_real_constraints(text: str) -> int:
    n = 0
    for raw in re.split(r"\n+", text or ""):
        line = raw.strip().lstrip("#-*>| ").strip()
        if not line:
            continue
        for sent in re.split(r"(?<=[.!?])\s+", line):
            sent = sent.strip()
            if len(sent) >= 8 and _CONSTRAINT_RE.search(sent.upper()):
                n += 1
    return n


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


class FakeClient:
    """Just needs to be truthy — _auto_ingest_changed_docs only checks
    `client is None` to decide whether memnos is reachable."""


@pytest.fixture(autouse=True)
def _isolate_user_conf(monkeypatch, tmp_path):
    # Same isolation as test_effective_config.py: a real
    # ~/.memnos/agents/tommy/tommy.conf on the dev machine must not leak
    # into these deterministic assertions.
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent.conf", raising=False)


# ---------------------------------------------------------------------------
# _git_diff_base
# ---------------------------------------------------------------------------


class TestGitDiffBase:
    def test_not_a_git_repo_returns_none(self, tmp_path):
        assert cli_mod._git_diff_base(tmp_path) is None

    def test_single_commit_repo_falls_back_to_root_commit(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "first")
        base = cli_mod._git_diff_base(tmp_path)
        head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert base == head  # the only commit IS the root

    def test_short_history_falls_back_to_oldest_commit_not_head_10(self, tmp_path):
        _init_repo(tmp_path)
        shas = []
        for i in range(4):  # fewer than 10 commits
            (tmp_path / f"f{i}.txt").write_text(str(i))
            _commit_all(tmp_path, f"commit {i}")
            shas.append(_git(tmp_path, "rev-parse", "HEAD").stdout.strip())
        base = cli_mod._git_diff_base(tmp_path)
        assert base == shas[0], "should fall back to the root commit, not crash or return HEAD~10"

    def test_long_history_prefers_head_10(self, tmp_path):
        _init_repo(tmp_path)
        for i in range(12):
            (tmp_path / f"f{i}.txt").write_text(str(i))
            _commit_all(tmp_path, f"commit {i}")
        expected = _git(tmp_path, "rev-parse", "HEAD~10").stdout.strip()
        assert cli_mod._git_diff_base(tmp_path) == expected


class TestChangedFilesSince:
    def test_lists_changed_files_between_base_and_head(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "unchanged.txt").write_text("same")
        _commit_all(tmp_path, "base")
        base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        (tmp_path / "changed.md").write_text("new content")
        _commit_all(tmp_path, "add changed.md")
        changed = cli_mod._changed_files_since(tmp_path, base)
        assert changed == ["changed.md"]

    def test_no_changes_returns_empty_list(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "only commit")
        head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert cli_mod._changed_files_since(tmp_path, head) == []


# ---------------------------------------------------------------------------
# _auto_ingest_changed_docs
# ---------------------------------------------------------------------------


def _repo_with_matching_adr(tmp_path: Path) -> Path:
    """A real git repo whose history change window contains one real ADR
    (genuine SHALL/MUST/SHOULD language) under docs/adr/, matching a
    design_docs glob of "docs/adr/*.md"."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("hello\n")
    _commit_all(repo, "initial")

    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-widget-loader.md").write_text(SAMPLE_ADR.read_text())
    (repo / "unrelated.py").write_text("print('not a design doc')\n")
    _commit_all(repo, "add ADR-0001 and an unrelated file")
    return repo


def _effective(project_root: Path, **yaml_overrides):
    yaml_text = yaml_overrides.pop("_yaml_text", None)
    if yaml_text is not None:
        (project_root / "tommy.yaml").write_text(yaml_text)
    return resolve_effective_config(project_root=project_root)


GATE_YAML = """
tommy:
  version: 1
memnos:
  namespace: "org:test:auto-ingest"
design_docs:
  - "docs/adr/*.md"
corpus:
  auto_ingest: true
"""


class TestAutoIngestChangedDocs:
    def test_ingests_real_design_doc_with_shall_must_language(self, tmp_path, monkeypatch, capsys):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text=GATE_YAML)
        assert effective.value("design_docs") == ["docs/adr/*.md"]

        sent = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            sent["namespace"] = namespace
            sent["name"] = name
            sent["text"] = text
            sent["kind"] = kw.get("kind")
            sent["git_sha"] = kw.get("git_sha")
            # Independently verify + count using the real extractor's regex —
            # proves this is genuinely SHALL/MUST content, not an arbitrary string.
            n = _count_real_constraints(text)
            return {"ok": True, "constraints": n, "ids": list(range(n))}

        monkeypatch.setattr(cli_mod, "_http_corpus_ingest", fake_ingest)
        monkeypatch.setattr(cli_mod, "_http_corpus_list", lambda *a, **k: {"ok": True, "sources": []})

        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)

        assert sent["namespace"] == "org:test:auto-ingest"
        assert sent["name"] == "docs/adr/0001-widget-loader.md"
        assert sent["kind"] == "doc"
        assert sent["git_sha"]  # a real commit sha was threaded through
        assert sent["text"] == SAMPLE_ADR.read_text(), "must send the real fixture content, unmodified"
        assert _count_real_constraints(sent["text"]) == 4, "sanity: the fixture really has 4 RFC-2119 sentences"

        out = capsys.readouterr().err
        assert "4 constraint(s) ingested" in out

    def test_unrelated_file_not_matching_glob_is_not_ingested(self, tmp_path, monkeypatch):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text=GATE_YAML)

        calls = []
        monkeypatch.setattr(cli_mod, "_http_corpus_ingest",
                             lambda *a, **k: calls.append(a[3]) or {"ok": True, "constraints": 0, "ids": []})
        monkeypatch.setattr(cli_mod, "_http_corpus_list", lambda *a, **k: {"ok": True, "sources": []})

        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)

        assert calls == ["docs/adr/0001-widget-loader.md"], "unrelated.py must never be sent to /corpus/ingest"

    def test_no_design_docs_configured_is_a_quiet_noop(self, tmp_path, monkeypatch, capsys):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text="tommy:\n  version: 1\ncorpus:\n  auto_ingest: true\n")
        assert effective.value("design_docs") == []

        called = []
        monkeypatch.setattr(cli_mod, "_http_corpus_ingest", lambda *a, **k: called.append(1))
        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)
        assert called == []

    def test_memnos_unreachable_is_a_visible_noop_not_a_crash(self, tmp_path, monkeypatch, capsys):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text=GATE_YAML)

        called = []
        monkeypatch.setattr(cli_mod, "_http_corpus_ingest", lambda *a, **k: called.append(1))
        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, None, repo)  # client=None

        assert called == []
        assert "memnos unreachable" in capsys.readouterr().err

    def test_non_git_directory_is_a_visible_noop_not_a_crash(self, tmp_path, monkeypatch, capsys):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        effective = _effective(not_a_repo, _yaml_text=GATE_YAML)

        called = []
        monkeypatch.setattr(cli_mod, "_http_corpus_ingest", lambda *a, **k: called.append(1))
        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), not_a_repo)

        assert called == []
        assert "not a git repository" in capsys.readouterr().err

    def test_deleted_file_in_diff_window_is_skipped_not_crashed(self, tmp_path, monkeypatch):
        repo = _repo_with_matching_adr(tmp_path)
        # Delete the ADR in a follow-up commit — it's still "changed" in the
        # diff window (git diff --name-only reports deletions too) but no
        # longer exists on disk at HEAD.
        (repo / "docs" / "adr" / "0001-widget-loader.md").unlink()
        _commit_all(repo, "remove ADR-0001")
        effective = _effective(repo, _yaml_text=GATE_YAML)

        called = []
        monkeypatch.setattr(cli_mod, "_http_corpus_ingest", lambda *a, **k: called.append(1))
        monkeypatch.setattr(cli_mod, "_http_corpus_list", lambda *a, **k: {"ok": True, "sources": []})

        # Must not raise.
        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)
        assert called == [], "a deleted file must never be POSTed to /corpus/ingest"

    def test_readonly_token_403_is_logged_and_swallowed(self, tmp_path, monkeypatch, capsys):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text=GATE_YAML)

        monkeypatch.setattr(cli_mod, "_http_corpus_ingest",
                             lambda *a, **k: {"ok": False, "error": "corpus ingest failed (403): forbidden for namespace"})
        monkeypatch.setattr(cli_mod, "_http_corpus_list", lambda *a, **k: {"ok": True, "sources": []})

        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)  # must not raise
        assert "ingest failed" in capsys.readouterr().err

    def test_silent_wipe_to_zero_is_warned_loudly(self, tmp_path, monkeypatch, capsys):
        repo = _repo_with_matching_adr(tmp_path)
        effective = _effective(repo, _yaml_text=GATE_YAML)

        monkeypatch.setattr(cli_mod, "_http_corpus_ingest",
                             lambda *a, **k: {"ok": True, "constraints": 0, "ids": []})
        monkeypatch.setattr(
            cli_mod, "_http_corpus_list",
            lambda *a, **k: {"ok": True, "sources": [
                {"name": "docs/adr/0001-widget-loader.md", "constraint_count": 4},
            ]},
        )

        cli_mod._auto_ingest_changed_docs(cli_mod.TommyConfig(), effective, FakeClient(), repo)
        err = capsys.readouterr().err
        assert "4 -> 0" in err
        assert "⚠" in err
