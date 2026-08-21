"""
Stand-in "tommy --mcp" process for test_dispatch_stdin_isolation.py
(memnos#132).

Runs the REAL, unmodified tommy.mcp_server.tommy_dispatch() end-to-end:
real tempfile prompt, real ControlServer, real subprocess.Popen of a stub
"claude" (fixtures/fake_claude.py). The only seams monkeypatched, in the
mcp_server module namespace, mirror sigint_driver.py's/
test_dispatch_core_prompt_parity.py's own convention:

  - all_harnesses() -> a single fake HarnessSpec pointing at fake_claude.py,
    so no real `claude` binary is required in CI.
  - mcp_server._cfg -> a TommyConfig built directly (bypasses
    TommyConfig.load()'s filesystem reads), same as
    test_dispatch_core_prompt_parity.py's isolated_cfg fixture.

The one thing this driver process does NOT fake: its OWN stdin. The test
launches this script with stdin connected to a real, live pipe held open by
a background writer that never closes it — the same shape as an MCP host's
live JSON-RPC stream. Before the fix, tommy_dispatch's internal Popen() left
stdin unset, so the harness child inherited this exact live pipe as its own
stdin. This driver's whole job is to call tommy_dispatch() for real while
that live pipe is genuinely sitting on fd 0, so the test can inspect what
the spawned fake_claude child actually saw on ITS stdin.
"""
from __future__ import annotations

import json
import os
import sys

import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.discovery.harnesses import HarnessSpec

_harness_script = os.environ["TOMMY_TEST_HARNESS_SCRIPT"]
_workspace = os.environ["TOMMY_TEST_WORKSPACE"]
_dispatch_result_file = os.environ["TOMMY_TEST_DISPATCH_RESULT_FILE"]

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


mcp_server_mod.all_harnesses = _fake_all_harnesses
mcp_server_mod._cfg = TommyConfig(harness="claude", skip_permissions=False)
mcp_server_mod._active_project = None

# The real call under test. async_run=False blocks until the dispatched
# fake_claude child exits, so this driver's own exit is deterministic —
# inject_memory=False needs no live memnos server.
result = mcp_server_mod.tommy_dispatch(
    task="memnos#132 regression driver: create /dev/null-scoped marker (unused; real work is proving stdin isolation, not filesystem effects)",
    harness="claude",
    workspace=_workspace,
    async_run=False,
    inject_memory=False,
)

with open(_dispatch_result_file, "w") as f:
    json.dump(result, f)

sys.exit(0)
