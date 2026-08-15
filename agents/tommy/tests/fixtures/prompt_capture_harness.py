"""
Stand-in "harness" process for test_dispatch_core_prompt_parity.py.

Launched by the REAL tommy.mcp_server.tommy_dispatch() (via a fake
HarnessSpec) exactly the way a real harness binary (claude, codex, ...)
would be — including the real --append-system-prompt-file {prompt_file}
argument substitution from launch_template.

Before mcp_server.py's background drain thread can unlink the temp prompt
file (it does so once this process's stdout closes, i.e. once it exits),
this script copies the file's content out to
$TOMMY_TEST_PROMPT_CAPTURE_FILE so the test can inspect exactly what the
real subprocess received, then exits 0.
"""
import os
import sys

args = sys.argv[1:]
prompt_file = None
for i, arg in enumerate(args):
    if arg == "--append-system-prompt-file" and i + 1 < len(args):
        prompt_file = args[i + 1]
        break

capture_path = os.environ["TOMMY_TEST_PROMPT_CAPTURE_FILE"]

content = ""
if prompt_file and os.path.exists(prompt_file):
    with open(prompt_file, "r") as f:
        content = f.read()

with open(capture_path, "w") as f:
    f.write(content)
    f.flush()

sys.exit(0)
