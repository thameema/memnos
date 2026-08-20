"""
Stand-in "harness" process for test_secret_shield_e2e.py (issue #115).

Launched by the REAL _launch_harness()/tommy_dispatch() (via a fake
HarnessSpec) exactly the way a real harness binary (claude, codex, ...)
would be. Captures two things to $TOMMY_TEST_ENV_CAPTURE_FILE (JSON) before
exiting 0:

  "env"    — this process's own environment, so the test can prove a
             resolved secret reached the launched subprocess's REAL
             environment (not just Tommy's own process) under its
             configured env-var name.
  "prompt" — the content of whatever file it was launched with (found via
             --append-system-prompt-file, matching prompt_capture_harness.py's
             convention), so the test can prove the same secret value is
             absent from the prompt Tommy built — the resolved value must
             reach (a) but never (b).
"""
import json
import os
import sys

args = sys.argv[1:]
prompt_file = None
for i, arg in enumerate(args):
    if arg == "--append-system-prompt-file" and i + 1 < len(args):
        prompt_file = args[i + 1]
        break

prompt_text = ""
if prompt_file and os.path.exists(prompt_file):
    with open(prompt_file, "r") as f:
        prompt_text = f.read()

capture_path = os.environ["TOMMY_TEST_ENV_CAPTURE_FILE"]
with open(capture_path, "w") as f:
    json.dump({"env": dict(os.environ), "prompt": prompt_text}, f)

sys.exit(0)
