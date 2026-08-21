"""
Regression tests for memnos#132.

*** THIS TEST FILE EXISTS BECAUSE tommy_dispatch SHIPPED FOR SOME PERIOD
*** UNABLE TO DELIVER ANY PROMPT TO THE claude HARNESS AT ALL. Every
*** existing test at the time mocked or stubbed the launch in a way that
*** never actually exercised claude's real non-interactive-mode contract,
*** so nothing caught it — a purely mocked/stubbed-harness test would NOT
*** have caught this bug, and must not be treated as sufficient proof
*** again. Do not delete, skip, or weaken these tests to make a release
*** green; if they are failing, the dispatch mechanism is genuinely
*** broken. See https://github.com/thameema/memnos/issues/132.

Root cause (verified against the real installed `claude` binary during the
investigation, see PR description for the transcript):

  1. `launch_template` for the "claude" harness only ever carried
     `--append-system-prompt-file {prompt_file}` — system-level context,
     never an actual "prompt" in claude's own `[options] [prompt]` CLI
     grammar. claude auto-enters non-interactive print mode whenever its
     own stdout is not a TTY (true for every tommy_dispatch launch), and
     print mode hard-requires a real prompt via stdin or a positional
     argument. With neither, every dispatch failed immediately:
     "Error: Input must be provided either through stdin or as a prompt
     argument when using --print".
  2. Neither launch path (`cli.py`'s `_launch_harness` nor
     `mcp_server.py`'s `tommy_dispatch`) set `stdin=` explicitly on its
     `Popen()` call, so the harness child inherited whatever Tommy's own
     real stdin was. Under `tommy --mcp`, that's the MCP host's live
     JSON-RPC pipe, which is held open indefinitely and never reaches
     EOF — verified (see fixtures/fake_claude.py's docstring) that the
     real `claude` binary then HANGS forever reading it, rather than
     erroring: a silently stuck dispatch, worse than the immediate-failure
     case above.

The fix (see tommy/discovery/harnesses.py's apply_prompt_arg() and
DISPATCH_TRIGGER_PROMPT, and both Popen() call sites' explicit `stdin=`):
append a minimal trailing trigger prompt to leave print-mode's input gate
(preserving --append-system-prompt-file's system-level framing — the
trigger is not a duplicate of the task content, which still arrives
entirely via the system-prompt file), and pin `stdin=` explicitly on every
launch path — DEVNULL for the always-headless MCP dispatch path, and
DEVNULL-unless-a-real-terminal (`sys.stdin.isatty()`) for the interactive
CLI path, so the harness child NEVER inherits a live host pipe under any
launch path.

Tests below use a permanent, in-repo stand-in "claude" binary
(fixtures/fake_claude.py) that reproduces both real behaviors above on a
bounded clock — no live Claude Code credentials, no real `claude` binary,
and no opt-in env var gate: this file is part of the plain
`agents/tommy/tests` suite `.github/workflows/ci.yml`'s `tommy-tests` job
already runs unconditionally on every push/PR (see that job's `pytest
agents/tommy/tests -q` step) — nothing here can silently stop running.
"""
from __future__ import annotations

import contextlib
import json
import os
import pty
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tommy.discovery.harnesses import (
    apply_prompt_arg,
    DISPATCH_TRIGGER_PROMPT,
    HARNESS_REGISTRY,
)

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
NEVER_CLOSING_WRITER = FIXTURES / "never_closing_writer.py"
MCP_DRIVER = FIXTURES / "mcp_dispatch_driver.py"
CLI_DRIVER = FIXTURES / "cli_dispatch_driver.py"

_DRIVER_EXIT_BUDGET = 20.0  # generous: covers fake_claude's own 2s stdin probe budget


# ---------------------------------------------------------------------------
# Shared helper: a fifo held open by a background writer that never closes
# it — the same shape as an MCP host's live stdio JSON-RPC stream.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _live_never_closing_pipe(tmp_path: Path):
    """Yields a raw, open-for-read fd connected to a fifo that a background
    writer process keeps writing to and never closes."""
    fifo_path = tmp_path / "live_host.fifo"
    os.mkfifo(str(fifo_path))
    writer = subprocess.Popen([sys.executable, str(NEVER_CLOSING_WRITER), str(fifo_path)])
    try:
        # Blocks until the writer's own open() (above) connects — after
        # this returns, both ends are live.
        read_fd = os.open(str(fifo_path), os.O_RDONLY)
        try:
            yield read_fd
        finally:
            os.close(read_fd)
    finally:
        writer.kill()
        writer.wait(timeout=5)


# ---------------------------------------------------------------------------
# 1. In-process: tommy_dispatch (MCP path) actually delivers a real prompt.
#    Does not require a live pipe — this is the bare-minimum "it doesn't
#    even error out immediately" regression guard, independent of the
#    deeper stdin-inheritance scenario below.
# ---------------------------------------------------------------------------

def test_dispatch_delivers_a_real_prompt_not_just_system_prompt_file(tmp_path, monkeypatch):
    """
    memnos#132 core regression: before the fix, tommy_dispatch's `claude`
    launch_template carried ONLY --append-system-prompt-file, with no
    positional prompt and no stdin content — the real claude binary exits 1
    immediately with "Input must be provided either through stdin or as a
    prompt argument when using --print" every single time, so no dispatched
    work ever actually ran. This test proves the real, unmodified
    tommy_dispatch() now hands the stand-in "claude" a genuine positional
    prompt argument, using the SAME code path (apply_prompt_arg, real
    Popen) production traffic goes through.
    """
    import tommy.mcp_server as mcp_server_mod
    from tommy.config import TommyConfig
    from tommy.discovery.harnesses import HarnessSpec

    result_file = tmp_path / "fake_claude_result.json"
    monkeypatch.setenv("TOMMY_TEST_RESULT_FILE", str(result_file))

    spec = HarnessSpec(
        name="claude",
        binary=str(FAKE_CLAUDE),
        launch_template=[
            str(FAKE_CLAUDE),
            "--append-system-prompt-file", "{prompt_file}",
        ],
        supports_tools=True,
        supports_mcp=False,
        description="test stand-in for the real claude CLI (memnos#132)",
        available=True,
    )
    monkeypatch.setattr(mcp_server_mod, "all_harnesses", lambda: {"claude": spec})
    monkeypatch.setattr(
        mcp_server_mod, "_cfg",
        TommyConfig(harness="claude", skip_permissions=False),
        raising=False,
    )
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)

    task_text = "MEMNOS-132-MARKER: prove the dispatch delivers a real prompt"
    result = mcp_server_mod.tommy_dispatch(
        task=task_text,
        harness="claude",
        workspace=str(tmp_path),
        async_run=False,
        inject_memory=False,
    )

    assert result.get("status") == "done", (
        f"dispatch did not exit cleanly — this is exactly the pre-fix "
        f"failure mode (claude erroring on 'no prompt provided'): {result}"
    )

    assert result_file.exists(), "fake_claude never ran or never wrote its result"
    captured = json.loads(result_file.read_text())

    assert captured["had_positional_prompt"] is True, (
        "no positional prompt argument reached the harness — this is "
        "memnos#132 itself: --append-system-prompt-file alone does not "
        "satisfy claude's print-mode input requirement"
    )
    assert captured["exit_code"] == 0
    assert task_text in captured["system_prompt_content"], (
        "the dispatched task text is missing from the system-prompt-file "
        "content — --append-system-prompt-file's system-level framing must "
        "still carry the actual task"
    )


# ---------------------------------------------------------------------------
# 2. Real subprocess, real live-pipe stdin: the MCP-host-inheritance
#    scenario from the issue's open question.
# ---------------------------------------------------------------------------

def test_dispatch_never_inherits_a_live_host_stdin_pipe(tmp_path):
    """
    memnos#132's open question, proven end-to-end: when Tommy runs as
    `tommy --mcp`, its own stdin is the MCP host's live JSON-RPC pipe,
    which never reaches EOF. Before the fix, tommy_dispatch's Popen() left
    `stdin=` unset, so the harness child inherited that exact live pipe —
    verified against the real `claude` binary that this makes it hang
    forever (see fixtures/fake_claude.py's docstring for the `timeout 12
    claude ... < fifo` -> exit 124 reproduction).

    This spawns a real driver subprocess (fixtures/mcp_dispatch_driver.py)
    whose OWN stdin is connected to a fifo held open by a background writer
    that never closes it (see _live_never_closing_pipe above) — reproducing
    the live-MCP-host-pipe shape exactly — then calls the real, unmodified
    tommy_dispatch() from inside it. The spawned fake_claude child's OWN
    stdin is then inspected: it must have reached EOF immediately (proving
    isolation from the live pipe, i.e. `stdin=subprocess.DEVNULL` really is
    in effect), not stayed open/blocked (which would reproduce the hang).
    """
    dispatch_result_file = tmp_path / "dispatch_result.json"
    fake_claude_result_file = tmp_path / "fake_claude_result.json"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    env = {
        **os.environ,
        "TOMMY_TEST_HARNESS_SCRIPT": str(FAKE_CLAUDE),
        "TOMMY_TEST_WORKSPACE": str(workspace),
        "TOMMY_TEST_DISPATCH_RESULT_FILE": str(dispatch_result_file),
        "TOMMY_TEST_RESULT_FILE": str(fake_claude_result_file),
    }

    with _live_never_closing_pipe(tmp_path) as live_stdin_fd:
        driver = subprocess.Popen(
            [sys.executable, str(MCP_DRIVER)],
            env=env,
            stdin=live_stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            out, _ = driver.communicate(timeout=_DRIVER_EXIT_BUDGET)
        except subprocess.TimeoutExpired:
            driver.kill()
            out, _ = driver.communicate()
            pytest.fail(
                f"driver did not exit within {_DRIVER_EXIT_BUDGET}s — "
                f"tommy_dispatch itself hung with a live host pipe on "
                f"stdin (the exact bug this test guards against)\n"
                f"driver output:\n{out.decode(errors='replace')}"
            )

    assert driver.returncode == 0, (
        f"driver exited {driver.returncode}\n{out.decode(errors='replace')}"
    )
    assert dispatch_result_file.exists(), "driver never reached tommy_dispatch's return"
    dispatch_result = json.loads(dispatch_result_file.read_text())
    assert dispatch_result.get("status") == "done", dispatch_result

    assert fake_claude_result_file.exists(), (
        "the harness child (fake_claude) never ran or never wrote its "
        "result file"
    )
    captured = json.loads(fake_claude_result_file.read_text())

    assert captured["had_positional_prompt"] is True, captured
    assert captured["exit_code"] == 0, (
        f"harness child did not exit cleanly: {captured} — exit_code 2 "
        f"means its stdin never reached EOF (the live host pipe leaked "
        f"through), exit_code 1 means no prompt was ever delivered"
    )
    assert captured["stdin_eof_immediately"] is True, (
        "the harness child's stdin did NOT reach EOF — the live, "
        "never-closing MCP-host-shaped pipe leaked into the dispatched "
        "subprocess. This is memnos#132's stdin-inheritance regression: "
        "the dispatch Popen() call must set stdin=subprocess.DEVNULL "
        "explicitly, never leave it unset/inherited."
    )


# ---------------------------------------------------------------------------
# 3. Real subprocess: the interactive CLI path (`tommy`, no args) must
#    NEVER let the harness inherit a live, non-TTY host pipe either, while
#    a genuinely interactive (real TTY) launch keeps working unchanged.
# ---------------------------------------------------------------------------

def test_cli_launch_never_inherits_a_live_non_tty_pipe(tmp_path):
    """
    memnos#132 applies to the interactive CLI path too whenever Tommy's own
    stdin is not a real terminal (`tommy` invoked from a script/cron/CI
    with stdin piped). Same proof shape as
    test_dispatch_never_inherits_a_live_host_stdin_pipe above, against
    cli.py's _launch_harness() instead of mcp_server.py's tommy_dispatch().
    """
    fake_claude_result_file = tmp_path / "fake_claude_result.json"
    env = {
        **os.environ,
        "TOMMY_TEST_HARNESS_SCRIPT": str(FAKE_CLAUDE),
        "TOMMY_TEST_RESULT_FILE": str(fake_claude_result_file),
    }

    with _live_never_closing_pipe(tmp_path) as live_stdin_fd:
        driver = subprocess.Popen(
            [sys.executable, str(CLI_DRIVER)],
            env=env,
            cwd=str(tmp_path),
            stdin=live_stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            out, _ = driver.communicate(timeout=_DRIVER_EXIT_BUDGET)
        except subprocess.TimeoutExpired:
            driver.kill()
            out, _ = driver.communicate()
            pytest.fail(
                f"driver did not exit within {_DRIVER_EXIT_BUDGET}s — "
                f"_launch_harness hung with a live, non-TTY pipe on stdin\n"
                f"driver output:\n{out.decode(errors='replace')}"
            )

    assert driver.returncode == 0, f"driver exited {driver.returncode}\n{out.decode(errors='replace')}"
    assert fake_claude_result_file.exists(), "the harness child never ran"
    captured = json.loads(fake_claude_result_file.read_text())

    assert captured["stdin_isatty"] is False, captured  # sanity: really not a TTY
    assert captured["had_positional_prompt"] is True, (
        "no trigger prompt was added on the non-interactive CLI branch — "
        f"claude would hit the same 'no prompt provided' failure: {captured}"
    )
    assert captured["exit_code"] == 0, captured
    assert captured["stdin_eof_immediately"] is True, (
        "the harness child's stdin did not reach EOF — the live pipe "
        "leaked through on the CLI launch path too: "
        f"{captured}"
    )


def test_cli_launch_fully_interactive_terminal_unchanged(tmp_path):
    """
    Non-regression companion to the two tests above: when Tommy's own stdin
    AND stdout genuinely ARE a terminal (the normal `tommy` with no args
    case, run directly at a shell prompt), _launch_harness must behave
    exactly as it did before memnos#132's fix — stdin inherited, no trigger
    prompt injected. Real claude's print-mode auto-detection is driven by
    STDOUT (see cli.py's comment at cmd's construction, and `claude --help`:
    "...or when stdout is not a TTY"), so this test connects BOTH ends of
    the driver to the same real pseudo-terminal (pty.openpty()) — matching
    a real terminal session, where a human can genuinely type into the
    launched claude REPL.

    Consequence: with no trigger prompt and a live (never-EOF, nothing
    written to it by this test) real-terminal stdin, fake_claude's stdin
    probe times out exactly like it would if a real claude were sitting at
    an interactive prompt waiting for the next keystroke — exit code 2 here
    represents "correctly waiting for human input", NOT the memnos#132 bug
    (which was about a HEADLESS launch's harness inheriting a live host
    pipe it was never meant to read from at all). Don't conflate the two:
    this test's job is to prove stdin is still genuinely inherited and no
    trigger was added — not to prove fake_claude exits 0.
    """
    fake_claude_result_file = tmp_path / "fake_claude_result.json"
    env = {
        **os.environ,
        "TOMMY_TEST_HARNESS_SCRIPT": str(FAKE_CLAUDE),
        "TOMMY_TEST_RESULT_FILE": str(fake_claude_result_file),
    }

    master_fd, slave_fd = pty.openpty()
    try:
        driver = subprocess.Popen(
            [sys.executable, str(CLI_DRIVER)],
            env=env,
            cwd=str(tmp_path),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)
        slave_fd = -1
        try:
            driver.wait(timeout=_DRIVER_EXIT_BUDGET)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.wait()
            pytest.fail(f"driver did not exit within {_DRIVER_EXIT_BUDGET}s on a real TTY stdin+stdout")
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)

    assert fake_claude_result_file.exists(), "the harness child never ran"
    captured = json.loads(fake_claude_result_file.read_text())

    assert captured["stdin_isatty"] is True, (
        "the harness child's stdin was not a TTY even though the driver's "
        f"own stdin genuinely was one — stdin is no longer being inherited "
        f"on the real-terminal branch: {captured}"
    )
    # No extra_args were passed, and stdout genuinely IS a TTY: no trigger
    # prompt should be injected — the interactive REPL experience (empty
    # first turn, human types first) must stay exactly as it was before the
    # fix.
    assert captured["had_positional_prompt"] is False, (
        "a trigger prompt was appended on the genuinely-interactive branch "
        f"— this would change the primary `tommy` entry point's UX: {captured}"
    )
    # exit_code 2 here == fake_claude's own stdin probe timed out with
    # nothing written to the pty's master end — the expected, CORRECT
    # stand-in for "waiting on human input at an interactive prompt", not a
    # failure. See docstring above.
    assert captured["exit_code"] == 2, captured
    assert driver.returncode == 2, "driver's own exit code must mirror the harness child's"


def test_cli_launch_interactive_stdin_but_piped_stdout_still_gets_trigger(tmp_path):
    """
    The exact edge case that a stdin-only check would get wrong: a human
    runs `tommy | tee log.txt` (or any `tommy | ...`) directly at a real
    terminal — stdin genuinely IS a TTY, but stdout is now a pipe. Real
    claude's print-mode auto-detection is driven by stdout, not stdin (see
    `claude --help`), so this must still get a trigger prompt even though
    stdin is a real terminal — proving cli.py's fix checks
    `sys.stdout.isatty()` for the trigger decision, not
    `sys.stdin.isatty()` (which only governs whether stdin itself is
    inherited).
    """
    fake_claude_result_file = tmp_path / "fake_claude_result.json"
    env = {
        **os.environ,
        "TOMMY_TEST_HARNESS_SCRIPT": str(FAKE_CLAUDE),
        "TOMMY_TEST_RESULT_FILE": str(fake_claude_result_file),
    }

    master_fd, slave_fd = pty.openpty()
    try:
        driver = subprocess.Popen(
            [sys.executable, str(CLI_DRIVER)],
            env=env,
            cwd=str(tmp_path),
            stdin=slave_fd,
            stdout=subprocess.PIPE,   # piped, unlike stdin — the whole point
            stderr=subprocess.STDOUT,
        )
        os.close(slave_fd)
        slave_fd = -1
        try:
            out, _ = driver.communicate(timeout=_DRIVER_EXIT_BUDGET)
        except subprocess.TimeoutExpired:
            driver.kill()
            out, _ = driver.communicate()
            pytest.fail(
                f"driver did not exit within {_DRIVER_EXIT_BUDGET}s\n"
                f"driver output:\n{out.decode(errors='replace')}"
            )
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)

    assert driver.returncode == 0, f"driver exited {driver.returncode}\n{out.decode(errors='replace')}"
    assert fake_claude_result_file.exists(), "the harness child never ran"
    captured = json.loads(fake_claude_result_file.read_text())

    assert captured["stdin_isatty"] is True, (
        "stdin should still be inherited (it's a real terminal) even "
        f"though stdout is piped: {captured}"
    )
    assert captured["had_positional_prompt"] is True, (
        "no trigger prompt was added even though stdout is piped — real "
        f"claude would still auto-enter print mode here and fail on 'no "
        f"prompt provided': {captured}"
    )
    assert captured["exit_code"] == 0, captured


# ---------------------------------------------------------------------------
# 4. Structural tripwires (requirement: statically assert the claude
#    HarnessSpec's built command always carries a real prompt-delivery
#    mechanism, so this exact regression can't silently reappear even
#    before a subprocess is spawned).
# ---------------------------------------------------------------------------

def test_claude_launch_template_still_carries_system_prompt_file():
    """Cheap tripwire: the claude harness must still inject the fused
    coordinator+task content via --append-system-prompt-file — the
    memnos#132 fix adds a trigger positional, it does not remove or replace
    this."""
    template = HARNESS_REGISTRY["claude"].launch_template
    assert "--append-system-prompt-file" in template, template
    assert "{prompt_file}" in template, template


def test_apply_prompt_arg_appends_a_real_positional_for_claude():
    """apply_prompt_arg() is the ONE mechanism that satisfies claude's
    print-mode input requirement on both launch paths — this locks in its
    contract directly, independent of exercising a real subprocess."""
    cmd = ["claude", "--append-system-prompt-file", "/tmp/p.md"]
    out = apply_prompt_arg(cmd, "claude", DISPATCH_TRIGGER_PROMPT)
    assert out[-1] == DISPATCH_TRIGGER_PROMPT, out
    assert not out[-1].startswith("-"), "the trailing arg must be a real positional, not a flag"
    assert DISPATCH_TRIGGER_PROMPT.strip() != "", "trigger prompt must not be empty"

    # No-op for a harness not known to need it, and for an empty trigger —
    # both must never silently produce a malformed command line.
    assert apply_prompt_arg(cmd, "codex", DISPATCH_TRIGGER_PROMPT) == cmd
    assert apply_prompt_arg(cmd, "claude", "") == cmd


def test_mcp_dispatch_and_cli_launch_both_call_apply_prompt_arg():
    """Structural tripwire alongside the runtime proofs above: both real
    call sites must actually invoke apply_prompt_arg() — catches an
    accidental revert/removal fast, without needing to spawn a subprocess."""
    mcp_src = (Path(__file__).parent.parent / "tommy" / "mcp_server.py").read_text()
    cli_src = (Path(__file__).parent.parent / "tommy" / "cli.py").read_text()
    assert "apply_prompt_arg(" in mcp_src, "tommy_dispatch no longer calls apply_prompt_arg"
    assert "apply_prompt_arg(" in cli_src, "_launch_harness no longer calls apply_prompt_arg"
    assert "stdin=subprocess.DEVNULL" in mcp_src, (
        "tommy_dispatch's Popen() call no longer pins stdin=subprocess.DEVNULL "
        "— this would reintroduce memnos#132's live-host-pipe inheritance"
    )
