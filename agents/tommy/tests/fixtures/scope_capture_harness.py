#!/usr/bin/env python3
"""
Stand-in "claude" binary for issue #136's dispatch-scoping tests.

Same "directly executable at cmd[0]" convention as fake_claude.py (see its
docstring for why: apply_skip_permissions/apply_session_name/
apply_prompt_arg/apply_setting_sources all splice flags relative to cmd[0]).

Captures, to $TOMMY_TEST_SCOPE_CAPTURE_FILE (JSON), everything issue #136's
tests need to check in one place, all read AT THE MOMENT THIS PROCESS RUNS
(before mcp_server.py's _drain_stdout / cli.py's _launch_harness finally
block restores/deletes the generated scoping files — that only happens
once this process exits and its stdout reaches EOF):

  "argv"                 — full argv (after the binary name); tests check
                            for "--setting-sources" here.
  "env"                   — this process's own environment; tests check
                            MEMNOS_NS / MEMNOS_TOKEN / MEMNOS_URL here.
  "prompt"                — --append-system-prompt-file's content.
  "mcp_json_text"         — raw text of ./.mcp.json in this process's CWD,
                            or None if it doesn't exist.
  "settings_local_text"   — raw text of ./.claude/settings.local.json, or
                            None if it doesn't exist.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
prompt_file = None
for i, a in enumerate(args):
    if a == "--append-system-prompt-file" and i + 1 < len(args):
        prompt_file = args[i + 1]
        break

prompt_text = ""
if prompt_file and os.path.exists(prompt_file):
    prompt_text = Path(prompt_file).read_text()

cwd = Path.cwd()


def _read(p: Path):
    return p.read_text() if p.exists() else None


capture = {
    "argv": args,
    "env": dict(os.environ),
    "prompt": prompt_text,
    "mcp_json_text": _read(cwd / ".mcp.json"),
    "settings_local_text": _read(cwd / ".claude" / "settings.local.json"),
}

capture_path = os.environ["TOMMY_TEST_SCOPE_CAPTURE_FILE"]
with open(capture_path, "w") as f:
    json.dump(capture, f)

sys.exit(0)
