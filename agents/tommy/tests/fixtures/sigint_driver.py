"""
Stand-in "tommy" process for test_launch_harness_sigint.py.

Runs the REAL, unmodified tommy.cli._launch_harness() end-to-end: real
tempfile prompt, real ControlServer, real subprocess.Popen/wait of a stub
harness (see sigint_harness_stub.py). The only two seams are monkeypatched
in the cli module namespace:

  - all_harnesses()  -> returns a single fake HarnessSpec pointing at the
                         stub harness script, so no real `claude`/`codex`
                         binary is required in CI.
  - ControlServer     -> subclassed only to record the ephemeral port it
                         binds to (TOMMY_TEST_CTRL_PORT_FILE), so the test
                         can later prove the listening socket was actually
                         closed. Behavior is otherwise untouched.

The test spawns this script with start_new_session=True so it becomes its
own process-group leader; the harness it Popens (without start_new_session,
per the #77 fix) inherits that same group. Sending SIGINT to that group is
exactly what a terminal Ctrl-C does.
"""
import os
import sys

import tommy.cli as cli_mod
from tommy.config import TommyConfig
from tommy.control import ControlServer as _RealControlServer
from tommy.discovery.harnesses import HarnessSpec

_port_file = os.environ["TOMMY_TEST_CTRL_PORT_FILE"]


class _SpyControlServer(_RealControlServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with open(_port_file, "w") as f:
            f.write(str(self.port))
            f.flush()


cli_mod.ControlServer = _SpyControlServer

_harness_script = os.environ["TOMMY_TEST_HARNESS_SCRIPT"]
_stub_spec = HarnessSpec(
    name="stub-harness",
    binary=sys.executable,
    launch_template=[sys.executable, _harness_script, "{prompt_file}"],
    supports_tools=False,
    supports_mcp=False,
    description="test stub harness",
    available=True,
)


def _fake_all_harnesses(*args, **kwargs):
    return {"stub-harness": _stub_spec}


cli_mod.all_harnesses = _fake_all_harnesses

cfg = TommyConfig(harness="stub-harness", skip_permissions=False)

# _launch_harness() calls sys.exit(exit_code) itself — this process's exit
# code IS the value under test for the clean-exit-path contract.
cli_mod._launch_harness(cfg, project_key=None, extra_args=(), memnos_client=None)
