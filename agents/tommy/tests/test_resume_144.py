"""
Tests for memnos#144: document + polish the existing --resume/--continue
passthrough, make Tommy's own session titles unique per launch, and add
claude_session_id capture to tommy_dispatch.

Mirrors test_dispatch_stdin_isolation.py's proof strategy (memnos#132): real
in-process calls into the REAL, unmodified _launch_harness()/tommy_dispatch()
against fixtures/fake_claude.py — a permanent, in-repo stand-in "claude"
binary that reports exactly what it observed (argv, the received --name and
--session-id values) — rather than a mock that could pass vacuously. No live
Claude Code credentials, no real `claude` binary, no opt-in env var gate:
this file is part of the plain `agents/tommy/tests` suite that
`.github/workflows/ci.yml`'s `tommy-tests` job already runs unconditionally.

Three things are proven here:

  1. Session-title uniqueness (issue's item 2): two consecutive launches
     for the SAME project produce two DIFFERENT --name values, on both the
     interactive CLI path (cli.py's _launch_harness) and the MCP dispatch
     path (mcp_server.py's tommy_dispatch) — the exact bug reproduced live
     during the investigation ("--resume 'Tommy | TEST' matches 2
     sessions") is a title COLLISION, so uniqueness is the right level to
     assert at (see this issue's PR description for a real, hand-run
     `claude --resume "<title>"` disambiguation proof against two real
     throwaway sessions using this same naming scheme — not automated here,
     same reasoning as test_dispatch_scope_integration.py's module
     docstring for why a real-claude-binary check stays manual).

  2. claude_session_id capture (issue's item 3): tommy_dispatch pre-assigns
     a UUID via `--session-id`, the EXACT value that reaches the harness's
     argv is asserted (not just "a flag was passed"), and that same value
     is retrievable afterward via tommy_status.

  3. Non-regression (issue's item 3 acceptance criterion): `tommy --resume
     <id>` / `tommy -c` extra_args passthrough is unchanged — the resumed
     flags still land at the END of cmd, after every flag/value pair
     _launch_harness itself adds (order-sensitive, same contract
     apply_prompt_arg's docstring already establishes for its own trailing
     arg).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _stub_spec():
    from tommy.discovery.harnesses import HarnessSpec

    return HarnessSpec(
        name="claude",
        binary=str(FAKE_CLAUDE),
        launch_template=[str(FAKE_CLAUDE), "--append-system-prompt-file", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=False,
        description="test stand-in for the real claude CLI (memnos#132/#144)",
        available=True,
    )


# ---------------------------------------------------------------------------
# 1a. CLI path: session title is unique per launch.
# ---------------------------------------------------------------------------

def test_cli_launch_session_title_unique_per_launch(tmp_path, monkeypatch):
    """cli.py's _launch_harness must give each launch a DIFFERENT --name,
    even for the identical project — before the fix, `_session_name` was a
    pure f"Tommy | {PROJECT}" with no per-launch component, so two launches
    for the same project produced byte-identical titles."""
    import tommy.cli as cli_mod
    from tommy.config import TommyConfig

    monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"claude": _stub_spec()})
    monkeypatch.chdir(tmp_path)

    cfg = TommyConfig(harness="claude", skip_permissions=False)

    names = []
    for i in range(2):
        result_file = tmp_path / f"fake_claude_result_{i}.json"
        monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))
        with pytest.raises(SystemExit) as exc_info:
            cli_mod._launch_harness(cfg, project_key="demo", extra_args=(), memnos_client=None)
        assert exc_info.value.code == 0, f"launch {i} did not exit cleanly"
        captured = json.loads(result_file.read_text())
        name = captured["session_name"]
        assert name, f"launch {i} got no --name at all"
        assert name.startswith("Tommy | DEMO | "), name
        names.append(name)

    assert names[0] != names[1], (
        f"two launches for the SAME project produced the IDENTICAL session "
        f"title {names[0]!r} — this is exactly the memnos#144 collision "
        f"('--resume \"Tommy | TEST\" matches 2 sessions') reproduced live "
        f"in the investigation"
    )


# ---------------------------------------------------------------------------
# 1b. MCP dispatch path: session title is unique per dispatch.
# ---------------------------------------------------------------------------

def test_mcp_dispatch_session_title_unique_per_dispatch(tmp_path, monkeypatch):
    """mcp_server.py's tommy_dispatch has its own, independent copy of the
    same title construction (_mcp_session_name) — must be fixed too, not
    just the CLI path, or claude's own title-based resume/picker still
    can't disambiguate between two dispatches into the same project."""
    import tommy.mcp_server as mcp_server_mod
    from tommy.config import TommyConfig

    monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"claude": _stub_spec()})
    monkeypatch.setattr(mcp_server_mod, "_cfg", TommyConfig(harness="claude", skip_permissions=False), raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", "demo", raising=False)

    names = []
    for i in range(2):
        result_file = tmp_path / f"fake_claude_result_{i}.json"
        monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))
        result = mcp_server_mod.tommy_dispatch(
            task=f"MEMNOS-144-MARKER dispatch {i}",
            harness="claude",
            workspace=str(tmp_path),
            async_run=False,
            inject_memory=False,
        )
        assert result.get("status") == "done", result
        captured = json.loads(result_file.read_text())
        name = captured["session_name"]
        assert name, f"dispatch {i} got no --name at all"
        assert name.startswith("Tommy | DEMO | "), name
        names.append(name)

    assert names[0] != names[1], (
        f"two tommy_dispatch calls for the SAME project produced the "
        f"IDENTICAL session title {names[0]!r} on the MCP path"
    )


# ---------------------------------------------------------------------------
# 2. claude_session_id: captured on dispatch, exact value, retrievable via
#    tommy_status.
# ---------------------------------------------------------------------------

def test_dispatch_captures_real_claude_session_id_retrievable_via_status(tmp_path, monkeypatch):
    """tommy_dispatch must pre-assign a real UUID via --session-id BEFORE
    launching, store it as Task.claude_session_id, and tommy_status must
    surface the SAME value — not merely confirm a --session-id flag was
    present somewhere in argv."""
    import tommy.mcp_server as mcp_server_mod
    from tommy.config import TommyConfig

    monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"claude": _stub_spec()})
    monkeypatch.setattr(mcp_server_mod, "_cfg", TommyConfig(harness="claude", skip_permissions=False), raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)

    result_file = tmp_path / "fake_claude_result.json"
    monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))

    dispatch_result = mcp_server_mod.tommy_dispatch(
        task="MEMNOS-144-MARKER: prove claude_session_id capture",
        harness="claude",
        workspace=str(tmp_path),
        async_run=False,
        inject_memory=False,
    )
    assert dispatch_result.get("status") == "done", dispatch_result

    captured = json.loads(result_file.read_text())
    argv_session_id = captured["session_id"]
    assert argv_session_id, "no --session-id value ever reached the harness's argv"
    assert _UUID_RE.match(argv_session_id), f"not a valid UUID: {argv_session_id!r}"

    task_id = dispatch_result["task_id"]
    status = mcp_server_mod.tommy_status(task_id)
    assert status.get("claude_session_id") == argv_session_id, (
        f"tommy_status's claude_session_id ({status.get('claude_session_id')!r}) "
        f"does not match the UUID that actually reached the harness's own "
        f"--session-id argv ({argv_session_id!r}) — these must be the exact "
        f"same value, not merely both non-empty"
    )
    # Sanity: also the exact value returned by direct Task lookup.
    t = mcp_server_mod._tasks[task_id]
    assert t.claude_session_id == argv_session_id


def test_dispatch_claude_session_id_absent_for_unsupported_harness(tmp_path, monkeypatch):
    """A harness not in _SUPPORTS_SESSION_ID (i.e. anything but "claude"
    today) must get an empty Task.claude_session_id, and tommy_status must
    NOT fabricate a claude_session_id key for it — apply_session_id() never
    actually put --session-id on that harness's argv, so claiming otherwise
    would be a lie about what was actually launched.

    Registered under the name "codex" specifically because both
    apply_session_id() (_SUPPORTS_SESSION_ID) and apply_prompt_arg()
    (_NEEDS_TRAILING_PROMPT_ARG, memnos#132) are scoped to {"claude"} only
    — a harness name outside that set gets neither --session-id nor a
    trigger positional, so fake_claude (standing in for it here) correctly
    sees no prompt at all and exits 1 exactly like the real memnos#132
    failure mode for an unfixed harness. That's expected and orthogonal to
    what this test checks: session-id absence, not dispatch success."""
    import tommy.mcp_server as mcp_server_mod
    from tommy.config import TommyConfig
    from tommy.discovery.harnesses import HarnessSpec

    non_claude_spec = HarnessSpec(
        name="codex",
        binary=str(FAKE_CLAUDE),
        launch_template=[str(FAKE_CLAUDE), "--append-system-prompt-file", "{prompt_file}"],
        supports_tools=True,
        supports_mcp=False,
        description="stand-in registered under a non-claude harness name",
        available=True,
    )
    monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"codex": non_claude_spec})
    monkeypatch.setattr(mcp_server_mod, "_cfg", TommyConfig(harness="codex", skip_permissions=False), raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)

    result_file = tmp_path / "fake_claude_result.json"
    monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))

    dispatch_result = mcp_server_mod.tommy_dispatch(
        task="MEMNOS-144-MARKER: non-claude harness gets no session-id",
        harness="codex",
        workspace=str(tmp_path),
        async_run=False,
        inject_memory=False,
    )
    # Not "done" — see docstring: codex gets no trigger positional either
    # (unrelated to this issue, memnos#132 scope), so the stub correctly
    # reports the same immediate-failure mode real claude would show an
    # unfixed harness. What matters here is session-id absence, asserted
    # below regardless of dispatch outcome.
    assert "failed" in dispatch_result.get("status", ""), dispatch_result

    captured = json.loads(result_file.read_text())
    assert captured["session_id"] is None, "codex should never receive --session-id"
    assert captured["session_name"] is None, "codex should never receive --name either (memnos#144 scope: claude-only)"

    task_id = dispatch_result["task_id"]
    status = mcp_server_mod.tommy_status(task_id)
    assert "claude_session_id" not in status, status


# ---------------------------------------------------------------------------
# 3. Non-regression: the accidental --resume/-c passthrough is unchanged.
# ---------------------------------------------------------------------------

def test_cli_resume_extra_arg_still_passed_through_verbatim_at_end(tmp_path, monkeypatch):
    """`tommy --resume <session-id>` must still work exactly as it did
    before this issue's changes: cli.py's main() passes any flag it doesn't
    recognize straight through as extra_args, and _launch_harness appends
    them to the END of cmd. This issue only changes the session TITLE and
    (on the MCP path only) adds --session-id — it must not touch this
    mechanism at all (see the issue's item 4: explicitly not promoting
    -r/-c to real click options in this pass)."""
    import tommy.cli as cli_mod
    from tommy.config import TommyConfig

    monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"claude": _stub_spec()})
    monkeypatch.chdir(tmp_path)

    cfg = TommyConfig(harness="claude", skip_permissions=False)
    result_file = tmp_path / "fake_claude_result.json"
    monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))

    fake_session_id = str(uuid.uuid4())
    with pytest.raises(SystemExit) as exc_info:
        cli_mod._launch_harness(
            cfg, project_key=None, extra_args=("--resume", fake_session_id), memnos_client=None,
        )
    assert exc_info.value.code == 0, "extra_args passthrough launch did not exit cleanly"

    captured = json.loads(result_file.read_text())
    argv = captured["argv"]
    assert argv[-2:] == ["--resume", fake_session_id], (
        f"--resume <id> must land at the END of argv, after every flag "
        f"_launch_harness itself adds — got {argv}"
    )
    # The CLI path must NEVER inject --session-id of its own: combined with
    # --resume, that is a real, empirically-verified hard error in the real
    # claude binary ("--session-id can only be used with --continue or
    # --resume if --fork-session is also specified") — see
    # apply_session_id()'s docstring in discovery/harnesses.py.
    assert captured["session_id"] is None, (
        f"the CLI launch path injected --session-id ({captured['session_id']!r}) "
        f"even though extra_args already carried a user --resume — this "
        f"would hard-error against the real claude binary"
    )


def test_cli_continue_short_flag_still_passed_through_verbatim(tmp_path, monkeypatch):
    """`tommy -c` (--continue) — the other accidental-passthrough flag named
    explicitly in the issue — must also still reach the harness verbatim."""
    import tommy.cli as cli_mod
    from tommy.config import TommyConfig

    monkeypatch.setattr(cli_mod, "all_harnesses", lambda: {"claude": _stub_spec()})
    monkeypatch.chdir(tmp_path)

    cfg = TommyConfig(harness="claude", skip_permissions=False)
    result_file = tmp_path / "fake_claude_result.json"
    monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))

    with pytest.raises(SystemExit) as exc_info:
        cli_mod._launch_harness(cfg, project_key=None, extra_args=("-c",), memnos_client=None)
    assert exc_info.value.code == 0

    captured = json.loads(result_file.read_text())
    assert captured["argv"][-1] == "-c", captured["argv"]


# ---------------------------------------------------------------------------
# 4. --help documents the passthrough (issue item 1).
# ---------------------------------------------------------------------------

def test_help_documents_resume_passthrough():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "from tommy.cli import main; main()", "--help"],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "--resume" in out
    assert "--continue" in out or "-c " in out or "-c\n" in out
    assert "session title" in out.lower() or "resume by title" in out.lower()
