"""Capture-semantics tests for the Claude Code hooks (`memnos hook recall|remember`) —
run against a stub HTTP server, no real memnos/DB needed.

Exists because of a field-found CRITICAL: the Stop hook only persisted user turns, so
everything the agent said (decisions, ticket IDs) was invisible across sessions. These
tests pin the contract: BOTH speakers are saved, identifiers survive, only the reply
after the LAST user message is captured, and noise prompts suppress the pair.
Run: python tests/test_hooks.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PORT = 8917
PASS = FAIL = 0
captured = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        body = b'{"context": "stub"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def hook(which, payload):
    env = dict(os.environ, MEMNOS_URL=f"http://127.0.0.1:{PORT}", MEMNOS_NS="test:hooks",
               MEMNOS_TOKEN="mnk_test", HOME=tempfile.mkdtemp(prefix="memnos_hooks_"))
    return subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", which],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=30)


def transcript(events):
    tp = tempfile.mktemp(suffix=".jsonl")
    with open(tp, "w") as f:
        f.write("\n".join(json.dumps(e) for e in events))
    return tp


def u(text):
    return {"type": "user", "message": {"content": text}}


def a(*texts):
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": t} for t in texts]}}


def main():
    print("=== hook capture semantics ===")
    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # --- remember: both speakers, identifiers, only-after-last-user ---
    captured.clear()
    tp = transcript([
        u("old question that should be superseded entirely"),
        a("old answer with OLD-999"),
        u("please raise the two follow-up tickets for the consent work"),
        a("Done — raised HPTE-471 and HPTE-472,"),
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x"},
            {"type": "text", "text": "anchored to HPTE-467 as the anchor ticket."}]}},
    ])
    hook("remember", {"prompt": "please raise the two follow-up tickets for the consent work",
                      "transcript_path": tp})
    check("remember: exactly two writes (user + assistant)",
          [b["speaker"] for _, b in captured] == ["user", "assistant"])
    atext = captured[-1][1]["text"] if captured else ""
    check("remember: assistant reply captured with identifiers verbatim",
          "HPTE-471" in atext and "HPTE-472" in atext and "HPTE-467" in atext)
    check("remember: streamed assistant events concatenated", "Done" in atext and "anchored" in atext)
    check("remember: only the reply AFTER the last user message", "OLD-999" not in atext)

    # --- remember: noise prompt suppresses BOTH writes ---
    captured.clear()
    tp = transcript([u("<task-notification>automated</task-notification>"),
                     a("a long automated-event reply that must not be stored either way")])
    hook("remember", {"prompt": "", "transcript_path": tp})
    check("remember: noise prompt → zero writes (assistant suppressed too)", len(captured) == 0)

    # --- remember: trivial assistant reply skipped, user still saved ---
    captured.clear()
    tp = transcript([u("a perfectly reasonable question with enough words"), a("ok")])
    hook("remember", {"prompt": "a perfectly reasonable question with enough words",
                      "transcript_path": tp})
    check("remember: trivial assistant reply skipped, user kept",
          [b["speaker"] for _, b in captured] == ["user"])

    # --- recall: posts the prompt, emits context ---
    captured.clear()
    r = hook("recall", {"prompt": "where does Alice work these days?"})
    check("recall: posts /recall with the prompt",
          captured and captured[0][0] == "/recall"
          and captured[0][1]["query"].startswith("where does Alice"))
    check("recall: emits additionalContext from the response", "stub" in r.stdout)

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
