"""
Corpus gate tests — issue #109.

tommy_dispatch (tommy/mcp_server.py) gets a pre-flight corpus check, gated
by tommy.yaml's corpus.corpus_gate, that runs BEFORE the harness launches
and injects the result as its own prompt layer ahead of the dispatched
task. Fail-open-but-visible (per the issue's amendment comment): a real
match, a real "nothing relevant" empty result, and a check that could not
run at all are three distinct, always-visible outcomes — none of them ever
blocks the dispatch.

Layer 1 (`TestFormatConstraintBlock`, `TestBuildPromptLayering`): pure unit
tests against _format_constraint_block() and build_prompt(), no subprocess.

Layer 2 (`TestTommyDispatchCorpusGate`): full tommy_dispatch() integration,
run for real against a stub harness (fixtures/prompt_capture_harness.py,
the same fixture test_dispatch_core_prompt_parity.py uses) so the assertion
is against what a real spawned subprocess actually received via
--append-system-prompt-file — not a reimplementation's internal state.
mcp_server._http_corpus_check (the tommy.corpus.corpus_check import) is
monkeypatched per the issue's own suggested test description ("mock
_corpus_check") rather than hitting a live memnos server: the underlying
/corpus/check endpoint's real extraction/ranking behavior against a real
Postgres is already covered by tests/test_corpus_api.py in the root suite
(and, end-to-end through Tommy's own HTTP layer, by
tests/test_corpus_gate_tommy.py) — these tests are about Tommy's own
plumbing (config resolution -> gate decision -> prompt injection -> visible
result), consistent with the tommy-tests CI job being documented as
DB-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tommy.config as config_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec
from tommy.prompt import build_prompt

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE_HARNESS_SCRIPT = FIXTURES / "prompt_capture_harness.py"


# ---------------------------------------------------------------------------
# Layer 1: _format_constraint_block() — pure formatting, no I/O
# ---------------------------------------------------------------------------


class TestFormatConstraintBlock:
    def test_match_case_is_prominent_and_lists_constraints(self):
        check = {"ok": True, "constraints": [
            {"source": "adr-1", "content": "All writes MUST go through the ORM.", "id": 1},
            {"source": "adr-2", "content": "PHI SHALL be encrypted at rest.", "id": 2},
        ]}
        block = mcp_server_mod._format_constraint_block(check)
        assert "Constraints to Check" in block
        assert "All writes MUST go through the ORM." in block
        assert "PHI SHALL be encrypted at rest." in block
        assert "adr-1" in block and "adr-2" in block

    def test_no_match_case_says_so_and_is_not_an_error(self):
        check = {"ok": True, "constraints": []}
        block = mcp_server_mod._format_constraint_block(check)
        assert "no relevant" in block.lower() or "no constraints" in block.lower()
        # May explicitly clarify "not a check failure" (disambiguation), but
        # must not be WORDED as a failure headline the way the check-failed
        # case is ("check failed", "could not run").
        assert "check failed" not in block.lower()
        assert "could not run" not in block.lower()

    def test_check_failed_case_is_distinct_from_no_match(self):
        failed = mcp_server_mod._format_constraint_block(
            {"ok": False, "constraints": [], "error": "connection refused"})
        empty = mcp_server_mod._format_constraint_block({"ok": True, "constraints": []})
        assert failed != empty
        assert "connection refused" in failed
        assert "could not run" in failed.lower() or "check failed" in failed.lower() or "failure" in failed.lower()
        # The no-match case must not contain failure language, and vice versa
        assert "no relevant" not in failed.lower()


# ---------------------------------------------------------------------------
# Layer 1b: build_prompt()'s constraint_block layer — positioning
# ---------------------------------------------------------------------------


class TestBuildPromptLayering:
    def _cfg(self):
        return TommyConfig(harness="claude", skip_permissions=False)

    def test_constraint_block_absent_when_not_passed(self):
        prompt = build_prompt(self._cfg(), task="do the thing")
        assert "corpus-gate" not in prompt

    def test_constraint_block_present_and_before_dispatched_task(self):
        prompt = build_prompt(
            self._cfg(), task="do the thing",
            constraint_block="## Constraints to Check\n\n- [adr-1] MUST use the ORM.",
        )
        assert "<!-- corpus-gate -->" in prompt
        assert "MUST use the ORM." in prompt
        gate_idx = prompt.index("<!-- corpus-gate -->")
        task_idx = prompt.index("<!-- dispatched-task -->")
        assert gate_idx < task_idx, "corpus-gate layer must come before the dispatched-task layer"

    def test_constraint_block_without_task_still_renders(self):
        # constraint_block is its own layer, independent of `task` being set —
        # own layer per the issue, not spliced into the task layer.
        prompt = build_prompt(self._cfg(), constraint_block="## Corpus Gate\n\nsomething")
        assert "<!-- corpus-gate -->" in prompt


# ---------------------------------------------------------------------------
# Layer 2: tommy_dispatch() end-to-end (real subprocess, mocked HTTP)
# ---------------------------------------------------------------------------


VALID_YAML_GATE_ON = """
tommy:
  version: 1
memnos:
  namespace: "org:test:corpus-gate"
corpus:
  corpus_gate: true
  auto_ingest: false
"""

VALID_YAML_GATE_OFF = """
tommy:
  version: 1
memnos:
  namespace: "org:test:corpus-gate"
corpus:
  corpus_gate: false
"""

# No memnos.namespace block at all — exercises the fallback to cfg.default_ns
# (the design decision this feature makes: the gate reads
# effective.value("namespace"), which falls through to tommy.conf's
# default_ns when tommy.yaml doesn't set memnos.namespace — see PR "Design
# decisions").
VALID_YAML_GATE_ON_NO_NAMESPACE = """
tommy:
  version: 1
corpus:
  corpus_gate: true
"""


@pytest.fixture(autouse=True)
def _isolate_user_conf(monkeypatch, tmp_path):
    # Same isolation as test_effective_config.py: a real
    # ~/.memnos/agents/tommy/tommy.conf on the dev machine must not leak
    # into these deterministic assertions.
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent.conf", raising=False)


@pytest.fixture
def isolated_cfg(monkeypatch):
    cfg = TommyConfig(harness="capture-harness", skip_permissions=False,
                       memnos_url="http://fake-memnos.invalid", memnos_token="test-token")
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
    return cfg


@pytest.fixture
def stub_harness(monkeypatch):
    spec = HarnessSpec(
        name="capture-harness",
        binary=sys.executable,
        launch_template=[
            sys.executable, str(CAPTURE_HARNESS_SCRIPT),
            "--append-system-prompt-file", "{prompt_file}",
        ],
        supports_tools=True,
        supports_mcp=False,
        description="test capture harness",
        available=True,
    )
    monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"capture-harness": spec})
    return spec


def _dispatch(tmp_path, monkeypatch, task_text="IMPLEMENT: refactor the widget loader"):
    capture_file = tmp_path / "captured_prompt.md"
    monkeypatch.setenv("TOMMY_TEST_PROMPT_CAPTURE_FILE", str(capture_file))
    result = mcp_server_mod.tommy_dispatch(
        task=task_text,
        harness="capture-harness",
        workspace=str(tmp_path),
        async_run=False,
        inject_memory=False,
    )
    captured = capture_file.read_text() if capture_file.exists() else ""
    return result, captured


class TestTommyDispatchCorpusGate:
    def test_gate_off_never_calls_corpus_check_and_injects_nothing(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML_GATE_OFF)
        called = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: called.append(1) or {"ok": True, "constraints": []})

        result, captured = _dispatch(tmp_path, monkeypatch)

        assert called == [], "corpus_gate=false must never call the corpus check"
        assert "corpus-gate" not in captured
        assert "corpus_gate" not in result

    def test_gate_off_by_default_with_no_tommy_yaml(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        # No tommy.yaml at all — corpus_gate defaults to off (effective_config's
        # DEFAULT_CORPUS_GATE), same as an explicit false.
        called = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: called.append(1) or {"ok": True, "constraints": []})

        result, captured = _dispatch(tmp_path, monkeypatch)

        assert called == []
        assert "corpus-gate" not in captured
        assert "corpus_gate" not in result

    def test_gate_on_with_matching_constraints_is_visible_and_does_not_block(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML_GATE_ON)
        seen_args = {}

        def fake_check(memnos_url, token, namespace, snippet, **kw):
            seen_args.update(memnos_url=memnos_url, token=token, namespace=namespace, snippet=snippet)
            return {"ok": True, "constraints": [
                {"source": "adr-widgets", "content": "Widget loaders MUST be idempotent.", "id": 7},
            ]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check", fake_check)

        result, captured = _dispatch(tmp_path, monkeypatch, task_text="IMPLEMENT: refactor the widget loader")

        # Visible in the prompt, prominently, ahead of the task
        assert "Widget loaders MUST be idempotent." in captured
        assert "Constraints to Check" in captured
        assert captured.index("<!-- corpus-gate -->") < captured.index("<!-- dispatched-task -->")

        # Dispatch was NOT blocked
        assert result.get("status") == "done", result

        # Visible in the returned dict too
        assert result["corpus_gate"]["ok"] is True
        assert result["corpus_gate"]["constraints"][0]["source"] == "adr-widgets"

        # Namespace used matches tommy.yaml's memnos.namespace (not cfg.default_ns)
        assert seen_args["namespace"] == "org:test:corpus-gate"
        assert seen_args["snippet"] == "IMPLEMENT: refactor the widget loader"

    def test_gate_on_with_no_matching_constraints_proceeds_and_says_so(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML_GATE_ON)
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: {"ok": True, "constraints": []})

        result, captured = _dispatch(tmp_path, monkeypatch)

        assert result.get("status") == "done", result
        assert result["corpus_gate"] == {"ok": True, "constraints": []}
        assert "corpus-gate" in captured
        assert "no relevant" in captured.lower() or "no constraints" in captured.lower()

    def test_gate_on_with_check_failure_proceeds_and_is_distinctly_visible(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        (tmp_path / "tommy.yaml").write_text(VALID_YAML_GATE_ON)
        monkeypatch.setattr(
            mcp_server_mod, "_http_corpus_check",
            lambda *a, **k: {"ok": False, "constraints": [], "error": "corpus check unreachable: ConnectError"},
        )

        result, captured = _dispatch(tmp_path, monkeypatch)

        # Dispatch still completes — fail-open
        assert result.get("status") == "done", result
        assert result["corpus_gate"]["ok"] is False
        assert "unreachable" in result["corpus_gate"]["error"]

        # Distinctly visible from the no-match case (different wording)
        assert "corpus-gate" in captured
        assert "could not run" in captured.lower() or "check failed" in captured.lower()
        assert "no relevant" not in captured.lower()

    def test_gate_on_but_broken_tommy_yaml_proceeds_and_surfaces_the_parse_error(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\nplatform:\n  foo: bar\n")
        called = []
        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check",
                             lambda *a, **k: called.append(1) or {"ok": True, "constraints": []})

        result, captured = _dispatch(tmp_path, monkeypatch)

        assert called == [], "a broken tommy.yaml must not fall through to a real corpus check"
        assert result.get("status") == "done", result
        assert result["corpus_gate"]["ok"] is False
        assert "tommy.yaml" in result["corpus_gate"]["error"]
        assert "corpus-gate" in captured

    def test_gate_on_falls_back_to_cfg_default_ns_when_yaml_has_no_namespace(
        self, isolated_cfg, stub_harness, tmp_path, monkeypatch,
    ):
        # tommy.yaml sets corpus_gate but no memnos.namespace — the real-user
        # shape this feature must not crash or misbehave on.
        (tmp_path / "tommy.yaml").write_text(VALID_YAML_GATE_ON_NO_NAMESPACE)
        seen_args = {}

        def fake_check(memnos_url, token, namespace, snippet, **kw):
            seen_args["namespace"] = namespace
            return {"ok": True, "constraints": []}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_check", fake_check)

        result, _ = _dispatch(tmp_path, monkeypatch)

        assert result.get("status") == "done", result
        assert seen_args["namespace"] == isolated_cfg.default_ns


class TestCorpusGateHelpersExist:
    """Structural tripwire (same convention as
    test_dispatch_core_prompt_parity.py's test_mcp_dispatch_uses_shared_build_prompt_loader):
    the issue explicitly names _corpus_check() and _format_constraint_block()
    as the helpers mcp_server.py must define. A refactor that inlines or
    renames them (even one that keeps behavior identical) should fail a fast,
    cheap test here rather than only be caught by grep during review."""

    def test_mcp_server_defines_the_issue_named_helpers(self):
        src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
        assert "def _corpus_check(" in src
        assert "def _format_constraint_block(" in src
