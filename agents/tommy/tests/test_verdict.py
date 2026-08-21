"""
tommy_verdict tests — issue #112.

`tommy_verdict` (tommy/mcp_server.py) diffs ONE already-dispatched
`tommy_dispatch` task (`git diff HEAD~1 HEAD` in the exact workspace that
task ran in, captured on the task registry at dispatch time) against the
architecture corpus, via memnos#105's real `/corpus/check_diff` verdict
endpoint — not `tommy_drift_sweep`'s keyword-matched `recall_fallback`.

Tests build `Task` objects directly and insert them into `mcp_server._tasks`
rather than going through a real `tommy_dispatch()` subprocess launch (that
launch path is already covered by test_corpus_gate.py /
test_dispatch_core_prompt_parity.py) — this suite is specifically about
`tommy_verdict`'s own plumbing: workspace resolution from the task record
(not the active project), the fail-open/fail-closed posture for
`merge_blocked`, and the ok/no_diff/unverified distinctions the issue's
acceptance criteria requires.

`mcp_server._http_corpus_check_diff` is monkeypatched (same monkeypoint
convention as test_corpus_gate.py's `_http_corpus_check` and
test_drift_sweep.py) — the real `/corpus/check_diff` endpoint behavior
against a real Postgres is already covered by
tests/test_corpus_diff_api.py in the root suite.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tommy.config as config_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.mcp_server import Task


# ---------------------------------------------------------------------------
# Real git repo helpers (mirrors test_drift_sweep.py's _init_repo/_git)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _repo_with_a_real_diff(tmp_path: Path, name: str = "repo") -> Path:
    """Two commits: an initial commit, then one that adds a file — HEAD~1..HEAD
    is a real, non-empty diff."""
    repo = tmp_path / name
    _init_repo(repo)
    (repo / "base.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "widget.py").write_text("def widget_loader():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add widget loader")
    return repo


def _repo_with_single_commit(tmp_path: Path, name: str = "single") -> Path:
    repo = tmp_path / name
    _init_repo(repo)
    (repo / "only.md").write_text("only commit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "only commit")
    return repo


def _repo_with_empty_second_commit(tmp_path: Path, name: str = "emptydiff") -> Path:
    """Two commits, but the second is `--allow-empty` — HEAD~1..HEAD exists
    and git succeeds, but the diff text itself is empty."""
    repo = tmp_path / name
    _init_repo(repo)
    (repo / "base.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "no-op task")
    return repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_user_conf(monkeypatch, tmp_path):
    # Same isolation as test_corpus_gate.py / test_drift_sweep.py: a real
    # ~/.memnos/agents/tommy/tommy.conf on the dev machine must not leak
    # into these deterministic assertions.
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent.conf", raising=False)


@pytest.fixture
def isolated_cfg(monkeypatch):
    cfg = TommyConfig(harness="claude", skip_permissions=False,
                       memnos_url="http://fake-memnos.invalid", memnos_token="test-token")
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_tasks", {}, raising=False)
    return cfg


def _register_task(workspace: Path, task_id: str = "t1", status_rc=0) -> Task:
    """Insert a Task directly into the registry (no real subprocess needed —
    tommy_verdict only reads .workspace and .status())."""
    proc = subprocess.Popen(["true" if status_rc == 0 else "false"])
    proc.wait()
    t = Task(task_id=task_id, harness="claude", proc=proc, workspace=str(workspace))
    mcp_server_mod._tasks[task_id] = t
    return t


YAML_MERGE_GATE_ON = """
tommy:
  version: 1
memnos:
  namespace: "org:test:verdict"
merge_gate: true
"""

YAML_MERGE_GATE_OFF = """
tommy:
  version: 1
memnos:
  namespace: "org:test:verdict"
merge_gate: false
"""


# ---------------------------------------------------------------------------
# Unknown task_id
# ---------------------------------------------------------------------------


class TestUnknownTask:
    def test_unknown_task_id_returns_error_same_shape_as_tommy_status(self, isolated_cfg):
        result = mcp_server_mod.tommy_verdict(task_id="does-not-exist")
        assert result == {"error": "Unknown task_id: 'does-not-exist'"}


# ---------------------------------------------------------------------------
# Workspace resolution: from the TASK record, not the active project
# ---------------------------------------------------------------------------


class TestWorkspaceComesFromTaskRecord:
    def test_diffs_the_workspace_the_task_was_dispatched_into_not_active_project(
        self, isolated_cfg, tmp_path, monkeypatch,
    ):
        task_repo = _repo_with_a_real_diff(tmp_path, name="task_repo")
        other_repo = _repo_with_a_real_diff(tmp_path, name="other_repo")
        (task_repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)

        # Simulate tommy_switch_project having changed the "active project"
        # to something else AFTER dispatch — must have zero effect here.
        monkeypatch.setattr(mcp_server_mod, "_active_project", "some-other-project", raising=False)

        seen_diffs = []
        monkeypatch.setattr(
            mcp_server_mod, "_http_corpus_check_diff",
            lambda url, token, ns, diff, **kw: seen_diffs.append(diff) or {
                "ok": True, "violated": [], "satisfied": [], "uncovered": [], "score": 1.0, "evaluated": 0,
            },
        )

        _register_task(task_repo, task_id="t1")
        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is True
        assert len(seen_diffs) == 1
        assert "widget_loader" in seen_diffs[0]  # the task_repo's diff, not other_repo's


# ---------------------------------------------------------------------------
# Git-level failures: distinct from a check that ran and found nothing
# ---------------------------------------------------------------------------


class TestGitDiffFailure:
    def test_single_commit_repo_is_unverified_not_a_crash_when_gate_on(
        self, isolated_cfg, tmp_path,
    ):
        repo = _repo_with_single_commit(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is False
        assert "error" in result
        assert result["violated"] == [] and result["satisfied"] == [] and result["uncovered"] == []
        assert result["score"] is None
        assert result["evaluated"] == 0
        assert result["merge_gate"] is True
        assert result["merge_blocked"] is True
        assert result["merge_blocked_reason"] == "unverified"

    def test_single_commit_repo_does_not_block_when_gate_off(self, isolated_cfg, tmp_path):
        repo = _repo_with_single_commit(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is False
        assert result["merge_gate"] is False
        assert result["merge_blocked"] is False
        assert result["merge_blocked_reason"] == "gate_off"

    def test_not_a_git_repo_at_all_is_unverified_not_a_crash(self, isolated_cfg, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        _register_task(not_a_repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is False
        assert result["merge_blocked"] is False  # merge_gate defaults off (no tommy.yaml)
        assert result["merge_blocked_reason"] == "gate_off"


# ---------------------------------------------------------------------------
# Empty diff (HEAD~1..HEAD exists but is empty) — distinct from "checked and clean"
# ---------------------------------------------------------------------------


class TestEmptyDiffIsNotAConfirmedCleanCheck:
    def test_empty_diff_never_calls_the_wrapper(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_empty_second_commit(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        called = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff",
                             lambda *a, **k: called.append(1) or {"ok": True})
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert called == [], "an empty diff must never be sent to /corpus/check_diff"
        assert result["ok"] is True
        assert result["evaluated"] == 0
        assert result["score"] is None
        assert result["violated"] == [] and result["satisfied"] == [] and result["uncovered"] == []
        assert "no diff" in result["note"].lower()
        assert "not the same as a check that ran" in result["note"].lower()
        assert result["merge_gate"] is True
        assert result["merge_blocked"] is False
        assert result["merge_blocked_reason"] == "no_diff"

    def test_empty_diff_with_gate_off_is_gate_off_not_no_diff_priority(
        self, isolated_cfg, tmp_path,
    ):
        repo = _repo_with_empty_second_commit(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["merge_blocked"] is False
        assert result["merge_blocked_reason"] == "gate_off"


# ---------------------------------------------------------------------------
# Real corpus_check_diff call: violated / clean / uncovered-only
# ---------------------------------------------------------------------------


class TestRealVerdict:
    def test_violations_with_gate_on_blocks_and_says_why(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": True,
            "violated": [{"id": 1, "content": "Widget loaders MUST NOT be synchronous.",
                           "source": "adr-1", "score": 0.7, "matched_terms": ["widget"],
                           "added_hits": 2, "removed_hits": 0}],
            "satisfied": [], "uncovered": [], "score": 0.0, "evaluated": 1,
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is True
        assert len(result["violated"]) == 1
        assert result["evaluated"] == 1
        assert result["merge_gate"] is True
        assert result["merge_blocked"] is True
        assert result["merge_blocked_reason"] == "violations"

    def test_violations_with_gate_off_never_blocks(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": True,
            "violated": [{"id": 1, "content": "X MUST NOT Y.", "source": "adr-1",
                           "score": 0.7, "matched_terms": [], "added_hits": 2, "removed_hits": 0}],
            "satisfied": [], "uncovered": [], "score": 0.0, "evaluated": 1,
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert len(result["violated"]) == 1  # still reported...
        assert result["merge_blocked"] is False  # ...but never blocks when gate is off
        assert result["merge_blocked_reason"] == "gate_off"

    def test_clean_result_with_gate_on_does_not_block(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": True,
            "violated": [], "satisfied": [{"id": 2, "content": "X SHALL Y.", "source": "adr-2",
                                             "score": 0.6, "matched_terms": [], "added_hits": 2,
                                             "removed_hits": 0}],
            "uncovered": [], "score": 1.0, "evaluated": 1,
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["merge_blocked"] is False
        assert result["merge_blocked_reason"] == "clean"

    def test_vacuous_and_real_one_point_zero_stay_distinguishable_via_evaluated(
        self, isolated_cfg, tmp_path, monkeypatch,
    ):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": True, "violated": [], "satisfied": [], "uncovered": [
                {"id": 3, "content": "Unrelated constraint.", "source": "adr-3", "score": 0.1,
                 "matched_terms": [], "added_hits": 0, "removed_hits": 0},
            ], "score": 1.0, "evaluated": 0,   # vacuous 1.0: nothing was evaluated
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["score"] == 1.0
        assert result["evaluated"] == 0   # a caller must NOT read this as "fully compliant"
        assert result["merge_blocked_reason"] == "clean"  # no violations => still not blocked


# ---------------------------------------------------------------------------
# /corpus/check_diff itself unreachable — distinguishable from "ran, clean"
# ---------------------------------------------------------------------------


class TestCorpusCheckDiffUnreachable:
    def test_unreachable_with_gate_on_fails_closed(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": False, "violated": [], "satisfied": [], "uncovered": [],
            "score": None, "evaluated": 0, "error": "corpus check_diff unreachable: ConnectError",
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is False
        assert "unreachable" in result["error"]
        assert result["merge_gate"] is True
        assert result["merge_blocked"] is True
        assert result["merge_blocked_reason"] == "unverified"

    def test_unreachable_with_gate_off_never_blocks(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": False, "violated": [], "satisfied": [], "uncovered": [],
            "score": None, "evaluated": 0, "error": "corpus check_diff unreachable: ConnectError",
        })
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["ok"] is False
        assert result["merge_blocked"] is False
        assert result["merge_blocked_reason"] == "gate_off"


# ---------------------------------------------------------------------------
# Broken tommy.yaml — merge_gate itself unknown
# ---------------------------------------------------------------------------


class TestBrokenTommyYaml:
    def test_broken_yaml_is_unverified_and_fails_closed_regardless_of_hypothetical_gate_value(
        self, isolated_cfg, tmp_path, monkeypatch,
    ):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text("tommy:\n  version: 1\nplatform:\n  foo: bar\n")
        called = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff",
                             lambda *a, **k: called.append(1) or {"ok": True})
        _register_task(repo, task_id="t1")

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert called == [], "a broken tommy.yaml must not fall through to a real corpus check"
        assert result["ok"] is False
        assert "tommy.yaml" in result["error"]
        # merge_gate is genuinely unknown here (the file that would say so
        # never parsed) — must be reported as None, never guessed True/False
        # just to justify the fail-closed merge_blocked below.
        assert result["merge_gate"] is None
        assert result["merge_blocked"] is True
        assert result["merge_blocked_reason"] == "unverified"


# ---------------------------------------------------------------------------
# Namespace resolution + name filter + task_status pass-through
# ---------------------------------------------------------------------------


class TestNamespaceAndNameAndStatus:
    def test_namespace_defaults_to_effective_config_namespace(
        self, isolated_cfg, tmp_path, monkeypatch,
    ):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        seen = {}
        monkeypatch.setattr(
            mcp_server_mod, "_http_corpus_check_diff",
            lambda url, token, ns, diff, **kw: seen.update(ns=ns, kw=kw) or {
                "ok": True, "violated": [], "satisfied": [], "uncovered": [], "score": 1.0, "evaluated": 0,
            },
        )
        _register_task(repo, task_id="t1")

        mcp_server_mod.tommy_verdict(task_id="t1")

        assert seen["ns"] == "org:test:verdict"

    def test_explicit_namespace_overrides_effective_config(
        self, isolated_cfg, tmp_path, monkeypatch,
    ):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        seen = {}
        monkeypatch.setattr(
            mcp_server_mod, "_http_corpus_check_diff",
            lambda url, token, ns, diff, **kw: seen.update(ns=ns) or {
                "ok": True, "violated": [], "satisfied": [], "uncovered": [], "score": 1.0, "evaluated": 0,
            },
        )
        _register_task(repo, task_id="t1")

        mcp_server_mod.tommy_verdict(task_id="t1", namespace="org:override")

        assert seen["ns"] == "org:override"

    def test_name_filter_passed_through(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_ON)
        seen = {}
        monkeypatch.setattr(
            mcp_server_mod, "_http_corpus_check_diff",
            lambda url, token, ns, diff, **kw: seen.update(kw) or {
                "ok": True, "violated": [], "satisfied": [], "uncovered": [], "score": 1.0, "evaluated": 0,
            },
        )
        _register_task(repo, task_id="t1")

        mcp_server_mod.tommy_verdict(task_id="t1", name="adr-widgets")

        assert seen["name"] == "adr-widgets"

    def test_task_status_is_reported(self, isolated_cfg, tmp_path, monkeypatch):
        repo = _repo_with_a_real_diff(tmp_path)
        (repo / "tommy.yaml").write_text(YAML_MERGE_GATE_OFF)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check_diff", lambda *a, **k: {
            "ok": True, "violated": [], "satisfied": [], "uncovered": [], "score": 1.0, "evaluated": 0,
        })
        _register_task(repo, task_id="t1", status_rc=0)

        result = mcp_server_mod.tommy_verdict(task_id="t1")

        assert result["task_status"] == "done"
        assert result["task_id"] == "t1"


class TestVerdictHelpersExist:
    """Structural tripwire (same convention as
    test_corpus_gate.py::TestCorpusGateHelpersExist): a refactor that inlines
    or renames tommy_verdict's shared helper should fail a fast, cheap test
    here rather than only be caught by grep during review."""

    def test_mcp_server_defines_tommy_verdict_and_its_helper(self):
        src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
        assert "def tommy_verdict(" in src
        assert "def _verdict_unverified(" in src
