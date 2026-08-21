"""
Stand-in "tommy" (interactive CLI) process for
test_dispatch_stdin_isolation.py (memnos#132).

Runs the REAL, unmodified tommy.cli._launch_harness() end-to-end against
fixtures/fake_claude.py, mirroring sigint_driver.py's monkeypatch
conventions (all_harnesses() swapped for a HarnessSpec pointing at the
stub). Whatever this driver's own stdin is when the test spawns it (a
real pty slave for the "genuinely interactive" case, or a plain pipe/fifo
for the "not a TTY" case) is exactly what _launch_harness()'s new
isatty()-based branch (see cli.py, memnos#132) has to route correctly.

_launch_harness() calls sys.exit(exit_code) itself — this process's exit
code IS the harness's exit code.
"""
from __future__ import annotations

import os
import sys

import tommy.cli as cli_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec

_harness_script = os.environ["TOMMY_TEST_HARNESS_SCRIPT"]

_stub_spec = HarnessSpec(
    name="claude",
    binary=_harness_script,
    launch_template=[
        _harness_script,
        "--append-system-prompt-file", "{prompt_file}",
    ],
    supports_tools=True,
    supports_mcp=False,
    description="test stand-in for the real claude CLI (memnos#132)",
    available=True,
)


def _fake_all_harnesses(*args, **kwargs):
    return {"claude": _stub_spec}


cli_mod.all_harnesses = _fake_all_harnesses

cfg = TommyConfig(harness="claude", skip_permissions=False)
cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
