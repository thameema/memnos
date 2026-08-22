"""
Stand-in "tommy" (interactive CLI) process for issue #136's dispatch-scoping
tests — mirrors cli_dispatch_driver.py's pattern exactly, pointed at
scope_capture_harness.py instead of fake_claude.py.

_launch_harness() calls sys.exit(exit_code) itself, so this must run as a
subprocess (an in-process pytest call would kill the test runner) — same
reasoning as cli_dispatch_driver.py.

Reads TommyConfig fields from env so the same driver script covers every
scenario (memnos_token set/unset) without needing a family of near-duplicate
driver scripts.
"""
from __future__ import annotations

import os

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
    supports_mcp=True,
    description="test stand-in for the real claude CLI (issue #136)",
    available=True,
)


def _fake_all_harnesses(*args, **kwargs):
    return {"claude": _stub_spec}


cli_mod.all_harnesses = _fake_all_harnesses

cfg = TommyConfig(
    harness="claude",
    skip_permissions=False,
    memnos_url=os.environ.get("TOMMY_TEST_MEMNOS_URL", "http://127.0.0.1:8900"),
    memnos_token=os.environ.get("TOMMY_TEST_MEMNOS_TOKEN") or None,
)
cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
