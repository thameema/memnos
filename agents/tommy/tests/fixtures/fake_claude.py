#!/usr/bin/env python3
"""
Stand-in "claude" binary for memnos#132's regression tests.

Executable directly (chmod +x + shebang above) so it can sit at cmd[0] of
the built command line exactly the way the real `claude` binary does —
apply_skip_permissions()/apply_session_name()/apply_prompt_arg() all
splice relative to cmd[0], so a stub that isn't itself directly executable
(e.g. `[sys.executable, "fake_claude.py", ...]`) would get flags spliced
into the WRONG place (e.g. `--name` landing on the python interpreter's own
argv instead of this script's).

Models the two real, empirically-verified behaviors of the actual `claude`
CLI in non-interactive (print) mode that this bug depended on — not a
reimplementation of claude, just its externally-observable stdin/argv
contract at the point that mattered:

  1. `claude --append-system-prompt-file <f>` with NO positional prompt
     argument, run with a closed/already-EOF stdin, exits 1 immediately
     with "Error: Input must be provided either through stdin or as a
     prompt argument when using --print" (reproduced verbatim against the
     real installed `claude` binary during the memnos#132 investigation).
  2. The same command with stdin instead connected to a live pipe that
     never reaches EOF (e.g. an MCP host's JSON-RPC stream, which the
     child previously inherited from Tommy's own stdin when nothing set
     `stdin=` explicitly) HANGS — verified against the real `claude`
     binary with a fifo held open by a never-closing background writer,
     `timeout 12 claude ... < fifo` exits 124.

This stub reproduces both observably, on a bounded clock, without ever
needing the real `claude` binary or credentials. It ALWAYS probes its own
stdin with a bounded `select.select([0], [], [], _STDIN_PROBE_BUDGET)`
first — deliberately not skipped just because a positional prompt is
present, since the whole point is to observe what fd 0 actually is
(diagnostic, reported in the result JSON either way) independent of
whether the separate "is there a prompt at all" gate below was satisfied:

  - A positional prompt argument present in argv -> "would have run
    successfully" regardless of what the stdin probe found -> writes the
    captured system-prompt-file content + argv + the stdin probe result to
    $TOMMY_TEST_RESULT_FILE as JSON, exits 0.
  - No positional prompt argument -> the stdin probe result decides the
    outcome:
      - readable AND a `read()` immediately returns b"" (real EOF, e.g.
        DEVNULL or an already-closed pipe) -> mirrors real claude's
        immediate failure: same error text, exit 1.
      - NOT readable within the budget, or readable but returns bytes
        without reaching EOF (a live, never-closing pipe) -> mirrors real
        claude's hang, but bounded: reports `stdin_eof_immediately: false`
        in the result JSON and exits 2 (distinct from claude's real exit 1)
        rather than actually blocking forever, so a regression fails a
        pytest assertion instead of hanging CI.
"""
from __future__ import annotations

import json
import os
import select
import sys

_STDIN_PROBE_BUDGET = 2.0  # seconds — generous vs. CI jitter, tiny vs. a real hang


def _find_flag_value(args: list[str], flag: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _has_positional_prompt(args: list[str]) -> bool:
    """True if argv (after the binary name) contains a bare token that is
    neither a known flag nor a known flag's value — i.e. a real positional
    prompt, exactly what claude's `[options] [prompt]` grammar needs to
    leave print-mode's input gate."""
    skip_next = False
    # memnos#144: --session-id added here too — a value-taking flag exactly
    # like --append-system-prompt-file/--name. Without this, its UUID value
    # would be misdetected as the positional prompt by this same allowlist
    # gap issue #134 (item 3) already flags as fragile; not fixing that
    # detection strategy here (out of scope, see #134), just keeping this
    # stub's allowlist in sync with the one new real flag this issue adds.
    known_value_flags = {"--append-system-prompt-file", "--name", "--session-id"}
    known_bare_flags = {"--dangerously-skip-permissions"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in known_value_flags:
            skip_next = True
            continue
        if arg in known_bare_flags:
            continue
        # Anything else is a positional.
        return True
    return False


def main() -> int:
    args = sys.argv[1:]
    result_path = os.environ.get("TOMMY_TEST_RESULT_FILE")

    prompt_file = _find_flag_value(args, "--append-system-prompt-file")
    system_prompt_content = ""
    if prompt_file and os.path.exists(prompt_file):
        with open(prompt_file, "r") as f:
            system_prompt_content = f.read()

    # memnos#144: capture whatever --session-id value (if any) this
    # invocation received, so a test can assert the exact UUID Tommy
    # generated actually reached the harness's argv — not just "a flag was
    # passed somewhere".
    session_id = _find_flag_value(args, "--session-id")
    session_name = _find_flag_value(args, "--name")

    had_positional = _has_positional_prompt(args)

    try:
        stdin_isatty = os.isatty(sys.stdin.fileno())
    except (OSError, ValueError):
        stdin_isatty = False

    # Always probe our own stdin — regardless of whether a positional
    # prompt was already given — with a bounded select() the way the real
    # claude binary's blocking read behaved in the investigation. This is
    # deliberately NOT gated on `had_positional`: the whole point of this
    # stub is to observe what fd 0 actually is (a fixed, already-EOF
    # source like DEVNULL, vs. a live, never-closing host pipe that leaked
    # through), independent of whether print-mode's separate "is there a
    # prompt at all" gate was satisfied. A regression that reintroduces
    # stdin inheritance must be caught even on a build where the prompt
    # argument fix is otherwise working correctly.
    eof_immediately = False
    try:
        readable, _, _ = select.select([sys.stdin], [], [], _STDIN_PROBE_BUDGET)
        if readable:
            chunk = os.read(sys.stdin.fileno(), 4096)
            eof_immediately = chunk == b""
        else:
            # Nothing to read within the budget — stdin is a live pipe with
            # no data flowing yet either; same "not EOF" outcome as data
            # arriving without ever closing.
            eof_immediately = False
    except OSError:
        eof_immediately = True

    result = {
        "argv": args,
        "had_positional_prompt": had_positional,
        "system_prompt_content": system_prompt_content,
        "stdin_isatty": stdin_isatty,
        "stdin_eof_immediately": eof_immediately,
        "session_id": session_id,
        "session_name": session_name,
        "exit_code": None,
    }

    if had_positional:
        # Real claude would now actually run the turn — print-mode's input
        # requirement is already satisfied by the positional argument,
        # independent of whatever the stdin probe above found.
        result["exit_code"] = 0
        if result_path:
            with open(result_path, "w") as f:
                json.dump(result, f)
        return 0

    if eof_immediately:
        # Verbatim match to the real claude error text (see this module's
        # docstring, reproduction 1).
        sys.stderr.write(
            "Error: Input must be provided either through stdin or as a "
            "prompt argument when using --print\n"
        )
        result["exit_code"] = 1
        if result_path:
            with open(result_path, "w") as f:
                json.dump(result, f)
        return 1

    # No positional prompt AND stdin never reached EOF within the budget —
    # the bounded stand-in for claude's real hang (reproduction 2). Exit
    # code 2 is deliberately distinct from claude's real exit 1 so a test
    # can tell "immediate failure" and "would have hung" apart.
    sys.stderr.write(
        "fake_claude: stdin did not reach EOF within "
        f"{_STDIN_PROBE_BUDGET}s — real claude would hang here "
        "(memnos#132)\n"
    )
    result["exit_code"] = 2
    if result_path:
        with open(result_path, "w") as f:
            json.dump(result, f)
    return 2


if __name__ == "__main__":
    sys.exit(main())
