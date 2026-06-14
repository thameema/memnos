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
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    # --- remember (headless `claude -p`): payload has no `prompt` and the transcript's
    # final assistant message isn't flushed; the reply lives in `last_assistant_message`.
    # Both sides must still be captured: user reconstructed from the transcript's last
    # user turn, assistant from `last_assistant_message`. ---
    captured.clear()
    tp = transcript([
        u("earlier headless question that should be superseded"),
        a("earlier headless answer with OLD-111"),
        u("in headless mode, what ticket did we file for the consent work"),
        # NOTE: no assistant event after the last user turn — not flushed at Stop time
    ])
    hook("remember", {  # NO `prompt` key — headless Stop payload omits it
        "transcript_path": tp,
        "last_assistant_message": "Filed HPTE-555 for the headless consent work.",
    })
    check("headless: both speakers saved (user reconstructed + assistant from payload)",
          [b["speaker"] for _, b in captured] == ["user", "assistant"])
    if captured:
        utext = captured[0][1]["text"]; atext = captured[1][1]["text"]
        check("headless: user turn is the transcript's LAST user message",
              "what ticket did we file" in utext and "superseded" not in utext)
        check("headless: assistant reply taken from last_assistant_message verbatim",
              "HPTE-555" in atext)
        check("headless: superseded earlier turn not leaked",
              "OLD-111" not in atext and "OLD-111" not in utext)

    # --- headless: last_assistant_message in content-block form is flattened ---
    captured.clear()
    tp = transcript([u("headless question delivered as a content-block reply please")])
    hook("remember", {"transcript_path": tp,
                      "last_assistant_message": [
                          {"type": "text", "text": "Block-form reply that is long enough OK-777."}]})
    check("headless: content-block last_assistant_message flattened to text",
          len(captured) == 2 and "OK-777" in captured[1][1]["text"])

    # --- interactive UNCHANGED: when the transcript HAS the flushed reply, the payload's
    # last_assistant_message must be ignored (fallback only fires when transcript empty). ---
    captured.clear()
    tp = transcript([
        u("interactive question with plenty of words to pass the filter"),
        a("Real transcript reply with TRUE-001."),
    ])
    hook("remember", {"prompt": "interactive question with plenty of words to pass the filter",
                      "transcript_path": tp,
                      "last_assistant_message": "WRONG-999 from payload must be ignored"})
    check("interactive unchanged: assistant comes from transcript, not last_assistant_message",
          len(captured) == 2 and "TRUE-001" in captured[1][1]["text"]
          and "WRONG-999" not in captured[1][1]["text"])

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

    # --- status (SessionStart): visible memory-ON line; warns when server down ---
    r = hook("status", {"source": "startup"})
    check("status: server up → memory ACTIVE systemMessage",
          "systemMessage" in r.stdout and "memory ACTIVE" in r.stdout)
    env = dict(os.environ, MEMNOS_URL="http://127.0.0.1:9", MEMNOS_NS="t", MEMNOS_TOKEN="mnk_x",
               HOME=tempfile.mkdtemp(prefix="memnos_hooks_"))
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", "status"],
                       input=json.dumps({"source": "startup"}), capture_output=True, text=True,
                       env=env, timeout=30)
    check("status: server down → visible OFF warning",
          "memory OFF" in r.stdout and "memnos start" in r.stdout)

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
