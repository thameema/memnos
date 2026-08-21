"""
Drift sweep tests — issue #110.

`tommy_drift_sweep` (tommy/mcp_server.py) checks recent commits against the
architecture corpus outside `tommy_dispatch`'s per-dispatch corpus gate
(issue #109) — e.g. commits made directly, or dispatched with the corpus
gate off.

Layer 1 (`TestClampCommits`, `TestChunkDiff`): pure unit tests against the
small helpers `tommy_drift_sweep` is built from, run over REAL temporary
git repos (including a real shallow clone) rather than mocked git output —
`git rev-list`/`git diff`'s actual behavior on short histories and shallow
clones is exactly what issue #110's acceptance criteria is about, so
asserting against a real repo is stronger evidence than asserting against a
hand-written stub of what git "should" output.

Layer 2 (`TestDriftSweepTool`): full `tommy_drift_sweep()` calls against a
real temp git repo, with `mcp_server._http_corpus_check` monkeypatched (the
same monkeypoint test_corpus_gate.py uses for the corpus gate) — this
suite is about Tommy's own plumbing (clamping, chunking, dedupe, mode
labeling), not the server's real FTS ranking, which
tests/test_corpus_api.py already covers end-to-end against a real Postgres.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tommy.config as config_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig


# ---------------------------------------------------------------------------
# Real git repo helpers (mirrors test_auto_ingest.py's _init_repo/_commit_all)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _word_bank(n: int) -> list:
    """`n` unique, purely-alphabetic 4+-char tokens (e.g. "ab" -> "abab"),
    deterministic and non-repeating — safe for the `[A-Za-z]{4,}` extraction
    regex core/store.py's real corpus_check() (and this file's own
    word-counting test) use to find "words" in a snippet."""
    import itertools
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = []
    for a, b in itertools.product(letters, letters):
        if len(words) >= n:
            break
        words.append((a + b) * 2)
    return words


def _make_repo_with_commits(tmp_path: Path, n: int, name: str = "repo", words_per_commit: int = 4) -> Path:
    """A real repo with exactly `n` commits, each ADDING a new file with
    `words_per_commit` globally-unique words.

    Each commit adds a new file (rather than repeatedly overwriting one)
    deliberately: `git diff A B` diffs TREES, not a stack of per-commit
    patches, so a history that keeps rewriting the same lines would
    produce a tiny total diff between any two points no matter how many
    commits sit in between — the opposite of what TestChunkDiff and the
    chunking-related tool tests below need to exercise."""
    repo = tmp_path / name
    _init_repo(repo)
    bank = _word_bank(n * words_per_commit)
    for i in range(n):
        words = bank[i * words_per_commit:(i + 1) * words_per_commit]
        (repo / f"note_{i}.md").write_text(f"# note {i}\n" + " ".join(words) + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"commit {i}")
    return repo


@pytest.fixture(autouse=True)
def _isolate_user_conf(monkeypatch, tmp_path):
    # A real ~/.memnos/agents/tommy/tommy.conf on the dev machine must not
    # leak into these deterministic assertions — same isolation as
    # test_corpus_gate.py / test_effective_config.py.
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent.conf", raising=False)


@pytest.fixture
def isolated_cfg(monkeypatch):
    cfg = TommyConfig(harness="claude", skip_permissions=False,
                       memnos_url="http://fake-memnos.invalid", memnos_token="test-token")
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
    return cfg


# ---------------------------------------------------------------------------
# Layer 1a: _clamp_commits() — real repos, including a real shallow clone
# ---------------------------------------------------------------------------


class TestClampCommits:
    def test_not_a_git_repo_returns_error_not_a_crash(self, tmp_path):
        effective_n, available_n, err = mcp_server_mod._clamp_commits(tmp_path, 20)
        assert err is not None
        assert effective_n == 0 and available_n == 0

    def test_single_commit_repo_clamps_to_zero_available(self, tmp_path):
        repo = _make_repo_with_commits(tmp_path, 1)
        effective_n, available_n, err = mcp_server_mod._clamp_commits(repo, 20)
        assert err is None
        assert available_n == 0   # only HEAD, no ancestors
        assert effective_n == 0   # clamped down, not a crash

    def test_repo_with_fewer_commits_than_requested_clamps(self, tmp_path):
        repo = _make_repo_with_commits(tmp_path, 5)
        effective_n, available_n, err = mcp_server_mod._clamp_commits(repo, 20)
        assert err is None
        assert available_n == 4   # 5 commits => 4 ancestors of HEAD
        assert effective_n == 4   # clamped to what's available

    def test_repo_with_more_commits_than_requested_does_not_clamp(self, tmp_path):
        repo = _make_repo_with_commits(tmp_path, 10)
        effective_n, available_n, err = mcp_server_mod._clamp_commits(repo, 3)
        assert err is None
        assert available_n == 9
        assert effective_n == 3   # the requested value, untouched

    def test_requested_zero_stays_zero(self, tmp_path):
        repo = _make_repo_with_commits(tmp_path, 10)
        effective_n, available_n, err = mcp_server_mod._clamp_commits(repo, 0)
        assert err is None
        assert effective_n == 0
        assert available_n == 9

    def test_negative_requested_clamps_to_zero(self, tmp_path):
        repo = _make_repo_with_commits(tmp_path, 10)
        effective_n, available_n, err = mcp_server_mod._clamp_commits(repo, -5)
        assert err is None
        assert effective_n == 0

    def test_real_shallow_clone_clamps_without_crashing(self, tmp_path):
        # A real `git clone --depth=N`, not a simulation — the shallow
        # boundary commit is grafted with no parents, which is exactly
        # what _clamp_commits' `git rev-list --count HEAD` needs to handle
        # correctly for this acceptance criterion (issue #110: "handles
        # shallow clones ... without crashing").
        src = _make_repo_with_commits(tmp_path, 8, name="src")
        shallow = tmp_path / "shallow"
        # `file://` is required: plain local-path clones silently ignore
        # --depth ("--depth is ignored in local clones; use file:// instead"),
        # which would make this test pass for the wrong reason (a full clone).
        _git(tmp_path, "clone", "-q", "--depth", "3", f"file://{src}", str(shallow))

        effective_n, available_n, err = mcp_server_mod._clamp_commits(shallow, 20)
        assert err is None
        assert available_n == 2   # 3 commits present locally => 2 ancestors of HEAD
        assert effective_n == 2

        # And the clamped diff itself must not crash git.
        diff, diff_err = mcp_server_mod._drift_git(shallow, "diff", f"HEAD~{effective_n}", "HEAD")
        assert diff_err is None


# ---------------------------------------------------------------------------
# Layer 1b: _chunk_diff() — pure, plus one real-repo coverage check
# ---------------------------------------------------------------------------


class TestChunkDiff:
    def test_empty_diff_yields_no_chunks(self):
        assert mcp_server_mod._chunk_diff("") == []

    def test_short_diff_is_a_single_chunk(self):
        text = "a" * 100
        chunks = mcp_server_mod._chunk_diff(text, chunk_chars=4000)
        assert chunks == [text]

    def test_long_diff_splits_into_multiple_chunks_that_reassemble(self):
        text = "x" * 10_000
        chunks = mcp_server_mod._chunk_diff(text, chunk_chars=4000)
        assert len(chunks) == 3
        assert "".join(chunks) == text
        assert all(len(c) <= 4000 for c in chunks)

    def test_real_diff_over_40_unique_words_needs_more_than_one_chunk_for_full_coverage(self, tmp_path):
        # Demonstrates the reason _chunk_diff exists: store.py's
        # corpus_check() only ever looks at a snippet's first 40 unique
        # 4+-letter words. A real multi-commit diff easily exceeds 40
        # unique words; chunking is what lets later words still get their
        # own FTS query instead of being silently ignored.
        repo = _make_repo_with_commits(tmp_path, 15)
        diff, err = mcp_server_mod._drift_git(repo, "diff", "HEAD~14", "HEAD")
        assert err is None
        import re
        unique_words = set(re.findall(r"[A-Za-z]{4,}", diff))
        assert len(unique_words) > 40
        chunks = mcp_server_mod._chunk_diff(diff, chunk_chars=200)
        assert len(chunks) > 1


# ---------------------------------------------------------------------------
# Layer 2: tommy_drift_sweep() end-to-end (real git, mocked HTTP)
# ---------------------------------------------------------------------------


class TestDriftSweepTool:
    def test_not_a_git_workspace_is_ok_false_not_a_crash(self, isolated_cfg, tmp_path):
        result = mcp_server_mod.tommy_drift_sweep(commits=20, workspace=str(tmp_path))
        assert result["ok"] is False
        assert "error" in result
        assert result["mode"] == "recall_fallback"
        # ok:False results carry the same key set as ok:True ones (all 0/False
        # rather than absent) so a caller can inspect either shape uniformly.
        assert result["commits_requested"] == 20
        assert result["commits_used"] == 0
        assert result["commits_available"] == 0
        assert result["clamped"] is False

    def test_clamping_is_reported_not_silent(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 5)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: {"ok": True, "constraints": []})

        result = mcp_server_mod.tommy_drift_sweep(commits=20, workspace=str(repo))

        assert result["ok"] is True
        assert result["commits_requested"] == 20
        assert result["commits_used"] == 4          # clamped to available ancestors
        assert result["commits_available"] == 4
        assert result["clamped"] is True

    def test_no_clamp_when_enough_history(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 10)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: {"ok": True, "constraints": []})

        result = mcp_server_mod.tommy_drift_sweep(commits=3, workspace=str(repo))

        assert result["commits_used"] == 3
        assert result["clamped"] is False

    def test_recall_fallback_mode_is_clearly_labeled_not_a_verdict(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 3)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check", lambda *a, **k: {
            "ok": True,
            "constraints": [{"source": "adr-1", "content": "Widgets MUST be idempotent.", "id": 1}],
        })

        result = mcp_server_mod.tommy_drift_sweep(commits=3, workspace=str(repo))

        assert result["mode"] == "recall_fallback"
        assert "possibly_relevant_constraints" in result
        assert result["possibly_relevant_constraints"][0]["content"] == "Widgets MUST be idempotent."
        # The note must explicitly disclaim verdict-mode confidence — but
        # must never assert, unqualified, that anything IS a violation.
        note = result.get("note", "")
        assert "verdict" in note.lower()
        assert "not a violated" in note.lower() or "not confirmed violations" in note.lower()
        assert "constraint violated" not in note.lower()
        assert "this commit violates" not in note.lower()

    def test_no_relevant_constraints_is_a_legitimate_empty_result(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 3)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: {"ok": True, "constraints": []})

        result = mcp_server_mod.tommy_drift_sweep(commits=3, workspace=str(repo))

        assert result["ok"] is True
        assert result["possibly_relevant_constraints"] == []
        assert result["check_failures"] == []

    def test_check_failure_is_visible_and_distinct_from_empty_result(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 3)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check", lambda *a, **k: {
            "ok": False, "constraints": [], "error": "connection refused",
        })

        result = mcp_server_mod.tommy_drift_sweep(commits=3, workspace=str(repo))

        assert result["ok"] is True   # the sweep itself ran (git succeeded)
        assert result["possibly_relevant_constraints"] == []
        assert result["check_failures"], "a corpus_check failure must be visible, not swallowed"
        assert "connection refused" in result["check_failures"][0]

    def test_chunking_calls_corpus_check_once_per_chunk_and_dedupes(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 15)
        # Force a small chunk size so the real diff definitely splits into
        # multiple chunks.
        monkeypatch.setattr(mcp_server_mod, "_DRIFT_CHUNK_CHARS", 200)

        calls = []

        def fake_check(memnos_url, token, namespace, snippet, **kw):
            calls.append(snippet)
            # Same constraint returned by every chunk — dedupe must collapse it to one.
            return {"ok": True, "constraints": [
                {"source": "adr-1", "content": "Widgets MUST be idempotent.", "id": 1},
            ]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check", fake_check)

        result = mcp_server_mod.tommy_drift_sweep(commits=14, workspace=str(repo))

        assert len(calls) > 1, "a large diff must be split across more than one corpus_check() call"
        assert result["chunks_checked"] == len(calls)
        assert len(result["possibly_relevant_constraints"]) == 1   # deduped across chunks

    def test_chunks_are_capped_and_the_cap_is_reported(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 15)
        monkeypatch.setattr(mcp_server_mod, "_DRIFT_CHUNK_CHARS", 10)
        monkeypatch.setattr(mcp_server_mod, "_DRIFT_MAX_CHUNKS", 2)

        calls = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: calls.append(1) or {"ok": True, "constraints": []})

        result = mcp_server_mod.tommy_drift_sweep(commits=14, workspace=str(repo))

        assert result["chunks_checked"] == 2
        assert len(calls) == 2
        assert result["chunks_available"] > 2
        assert result["chunks_truncated"] is True

    def test_commits_requested_zero_produces_no_diff_and_no_checks(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 5)
        calls = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: calls.append(1) or {"ok": True, "constraints": []})

        result = mcp_server_mod.tommy_drift_sweep(commits=0, workspace=str(repo))

        assert result["ok"] is True
        assert result["commits_used"] == 0
        assert result["diff_chars"] == 0
        assert result["chunks_checked"] == 0
        assert calls == []
        # A no-diff-to-check result must not read like "checked and found
        # nothing" (test_no_relevant_constraints_is_a_legitimate_empty_result
        # below) — both leave possibly_relevant_constraints == [], so the
        # `note` text is the only thing that keeps the two distinguishable.
        assert "no diff" in result["note"].lower()
        assert "not the same as a check that ran" in result["note"].lower()

    def test_namespace_override_is_passed_through(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 3)
        seen_ns = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda url, token, namespace, snippet, **k: seen_ns.append(namespace)
                             or {"ok": True, "constraints": []})

        mcp_server_mod.tommy_drift_sweep(commits=3, namespace="org:custom", workspace=str(repo))

        assert seen_ns and all(ns == "org:custom" for ns in seen_ns)

    def test_default_namespace_falls_back_to_effective_namespace(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _make_repo_with_commits(tmp_path, 3)
        seen_ns = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda url, token, namespace, snippet, **k: seen_ns.append(namespace)
                             or {"ok": True, "constraints": []})

        mcp_server_mod.tommy_drift_sweep(commits=3, workspace=str(repo))

        assert seen_ns and all(ns == isolated_cfg.default_ns for ns in seen_ns)
