"""
Regression test: tommy_dispatch (the MCP path) must inject the same
core.md-derived coordinator prompt that the interactive `tommy` CLI path
injects via tommy.cli._launch_harness() -> tommy.prompt.build_prompt().

Before this fix, tommy_dispatch (tommy/mcp_server.py) wrote ONLY the task
string (optionally memory-prefixed) to the temp file passed to the harness
via --append-system-prompt-file — core.md, the org/project/workspace-local
layers, the runtime-config block, and the MCP manifest were never built or
injected on this path. A harness dispatched through tommy_dispatch got
memory-primed but ran with none of the "thin coordinator" discipline
(dispatch work rather than implement it, leases before touching shared work
items, corpus_check before dispatch, wave-based fan-out) that the
interactive CLI path enforces on every launch.

The fix: tommy_dispatch now calls the SAME tommy.prompt.build_prompt() the
CLI path calls — not a reimplementation — passing the dispatched task as
build_prompt()'s new optional `task` layer (see tommy/prompt.py).

This test proves parity at the point that actually matters: not that
core.md "gets read" in isolation, but that its literal on-disk content
physically lands in the file argument a REAL, spawned harness subprocess
receives via --append-system-prompt-file, for BOTH entry points:

  1. The CLI path: tommy.prompt.build_prompt(), called exactly as
     tommy.cli._launch_harness() calls it (see test_launch_harness_sigint.py
     for the process-supervision half of that path — this file only tests
     prompt content).
  2. The MCP path: tommy.mcp_server.tommy_dispatch(), run for real against a
     stub "harness" binary (fixtures/prompt_capture_harness.py) that copies
     whatever file it receives via --append-system-prompt-file out to a
     capture file before mcp_server.py's drain thread can unlink the temp
     prompt file.

Scope note: this targets prompt-content parity, not process supervision
(covered by test_launch_harness_sigint.py) or live memnos wiring —
inject_memory=False is passed to tommy_dispatch so this test has no
dependency on a reachable memnos server.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec
from tommy.prompt import build_prompt

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE_HARNESS_SCRIPT = FIXTURES / "prompt_capture_harness.py"
CORE_MD = Path(__file__).parent.parent / "tommy" / "prompts" / "core.md"

# Markers unique to core.md's coordinator contract — the specific behavioral
# rules called out by the parity requirement: thin coordinator identity,
# leases before touching shared work items, corpus_check before dispatch,
# wave-based fan-out.
_COORDINATOR_MARKERS = [
    "You are Tommy, a personal orchestrator",
    "You do NOT implement, review, or investigate",
    "lease_acquire(key=",
    "corpus_check(snippet=",
    "Spawn Bounds — Max 4 Tasks Per Turn",
]


def _assert_has_coordinator_markers(text: str, where: str) -> None:
    missing = [m for m in _COORDINATOR_MARKERS if m not in text]
    assert missing == [], f"{where} is missing core.md coordinator markers: {missing}"


@pytest.fixture
def isolated_cfg(monkeypatch):
    """
    A TommyConfig built directly — bypassing TommyConfig.load()'s filesystem
    reads of ~/.memnos/agents/tommy/tommy.conf, which may or may not exist on
    the machine running this test — so both paths compare against an
    identical, hermetic config. `prompts_dir` defaults (via config.py's
    field factory) to the bundled tommy/prompts/ directory, i.e. the actual
    core.md the package ships.
    """
    cfg = TommyConfig(harness="capture-harness", skip_permissions=False)
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
    return cfg


@pytest.fixture
def stub_harness(monkeypatch):
    """Point all_harnesses() (as seen inside mcp_server.py's own namespace,
    mirroring how test_launch_harness_sigint.py's driver patches it in
    cli.py's namespace) at the real, spawnable capture-harness stub instead
    of requiring a real claude/codex binary on PATH."""
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


def test_core_md_reaches_cli_path(isolated_cfg):
    """
    Baseline: build_prompt() — what _launch_harness() calls on the
    interactive CLI path — really does inject core.md's literal content.
    This is the known-working side of the original asymmetry; asserting it
    here (against the same isolated_cfg used below) makes the MCP-path
    assertion in test_core_md_reaches_mcp_dispatch_path a fair
    apples-to-apples comparison, not just a standalone claim.
    """
    prompt = build_prompt(isolated_cfg, project_key=None)
    core_text = CORE_MD.read_text().strip()
    assert core_text in prompt, "build_prompt() does not contain core.md's literal content"
    _assert_has_coordinator_markers(prompt, "CLI path (build_prompt())")


def test_core_md_reaches_mcp_dispatch_path(isolated_cfg, stub_harness, tmp_path, monkeypatch):
    """
    The regression proof: tommy_dispatch's real subprocess launch must
    receive core.md's literal content via --append-system-prompt-file, the
    same as the CLI path — not just the bare task string.
    """
    capture_file = tmp_path / "captured_prompt.md"
    monkeypatch.setenv("TOMMY_TEST_PROMPT_CAPTURE_FILE", str(capture_file))

    task_text = "IMPLEMENT-TASK-MARKER: refactor the parity checker"
    result = mcp_server_mod.tommy_dispatch(
        task=task_text,
        harness="capture-harness",
        workspace=str(tmp_path),
        async_run=False,       # block until the stub harness exits — deterministic
        inject_memory=False,   # no live memnos server required for this test
    )

    assert result.get("status") == "done", f"stub harness did not exit cleanly: {result}"

    assert capture_file.exists(), (
        "capture-harness never received a --append-system-prompt-file argument, "
        "or never ran — tommy_dispatch's subprocess launch is broken"
    )
    captured = capture_file.read_text()

    core_text = CORE_MD.read_text().strip()
    assert core_text in captured, (
        "core.md's literal content is NOT present in the prompt file "
        "tommy_dispatch handed to the real subprocess — the MCP dispatch "
        "path still lacks the coordinator contract the interactive CLI path "
        "enforces"
    )
    _assert_has_coordinator_markers(captured, "MCP path (tommy_dispatch())")

    # The dispatched task itself must still be present — the fix must not
    # trade the task away to make room for core.md.
    assert task_text in captured, "dispatched task text missing from the prompt file"

    # Headless framing: core.md alone tells the session to greet the user and
    # permits asking a clarifying question — neither is answerable with no
    # human on the other end of a tommy_dispatch call.
    assert "Non-interactive dispatch" in captured, (
        "dispatched prompt does not override core.md's interactive-session "
        "assumptions (session-start greeting, clarifying questions) for a "
        "headless run"
    )


def test_mcp_dispatch_uses_shared_build_prompt_loader():
    """
    Cheap structural tripwire alongside the runtime proof above: mcp_server.py
    must call the shared tommy.prompt.build_prompt() loader rather than a
    reimplementation. Catches an accidental revert/duplication fast, without
    needing to spawn a subprocess.
    """
    src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
    assert "from .prompt import build_prompt" in src, (
        "mcp_server.py no longer imports the shared build_prompt() loader"
    )
    assert "build_prompt(" in src, (
        "mcp_server.py no longer calls build_prompt() — tommy_dispatch would "
        "regress to not loading core.md"
    )
