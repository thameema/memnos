"""issue #37 Layer 3 — TEST 2: recall during an outage returns a snapshot explicitly
labeled STALE, never silence and never a divergent/local answer.

Label choice (checked against the codebase's OWN existing conventions before picking
one — grep evidence, not invented): `core/service.py`/`memnos_server.py` already use a
`"degraded": true` JSON field, but that means something else — a LIVE server-side
pipeline that skipped a stage under a latency deadline (see core/service.py's
`_degraded`/`_mark_stale_turns`). Overloading it here (client served a CACHED response
because the server never answered at all) would make that existing signal ambiguous.
Both consumers modified for Layer 3 return plain TEXT anyway (MCP `recall()` returns a
string; the hook returns `additionalContext`/`systemMessage` strings), so the label
lives in the text itself, using the word "stale" (the issue's own acceptance wording)
plus the snapshot's timestamp — see memnos_cli.py's recall hook and memnos_mcp.recall().

Covers both consumers:
  1. The Claude Code hook (`memnos hook recall`) — stub HTTP server, one subprocess per
     call (same harness as tests/test_hooks.py).
  2. The MCP adapter's `recall()` tool — same-process, one isolated HOME throughout (no
     reload) so nsresolve's module-level cache paths can't go stale across scenarios.

Run: python tests/test_write_behind_recall_stale.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PY = sys.executable
PASS = FAIL = 0
CANNED_CONTEXT = "- (said) Thameem prefers dark roast coffee."


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


class RecallStub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"context": CANNED_CONTEXT, "memories": [{"content": CANNED_CONTEXT}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_stub():
    srv = HTTPServer(("127.0.0.1", 0), RecallStub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def dead_port_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                          # bound-then-closed: guaranteed connection-refused
    return f"http://127.0.0.1:{port}"


# ---- part 1: the Claude Code hook (`memnos hook recall`) ----------------------------

def hook_recall(home, url, ns, prompt, session_id="sess-stale-1"):
    env = dict(os.environ, HOME=home, MEMNOS_URL=url, MEMNOS_NS=ns, MEMNOS_TOKEN="mnk_test")
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", "recall"],
                       input=json.dumps({"prompt": prompt, "session_id": session_id}),
                       capture_output=True, text=True, env=env, timeout=30)
    out = {}
    if r.stdout.strip():
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    return r, out


def test_hook():
    print("=== hook: `memnos hook recall` serves a labeled-STALE snapshot on outage ===")
    srv, url = start_stub()
    home = tempfile.mkdtemp(prefix="memnos_wb_stale_hook_")
    ns = "test:wb-stale-hook"

    # 1. live recall while the server answers: normal context, NOT stale.
    r1, out1 = hook_recall(home, url, ns, "what coffee does Thameem like?", session_id="s1")
    ctx1 = (out1.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("live recall: succeeded (rc 0)", r1.returncode == 0)
    check("live recall: context contains the canned fact", CANNED_CONTEXT in ctx1)
    check("live recall: NOT labeled stale", "STALE" not in ctx1 and 'stale="true"' not in ctx1)
    snap_path = os.path.join(home, ".memnos", "recall_snapshot", "test_wb-stale-hook.json")
    check("live recall: saved a local last-known-good snapshot for this namespace",
          os.path.exists(snap_path))

    # 2. server goes down; a NEW session queries the SAME namespace.
    srv.shutdown()
    r2, out2 = hook_recall(home, dead_port_url(), ns, "what coffee does Thameem like?", session_id="s2")
    ctx2 = (out2.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("outage recall: still exits 0 (never breaks the session)", r2.returncode == 0)
    check("outage recall: explicitly labeled STALE in the text", "STALE" in ctx2)
    check("outage recall: carries the stale=\"true\" envelope attribute",
          'stale="true"' in ctx2)
    check("outage recall: still serves the SAME cached fact (from the SAME store, not "
          "silence, not a divergent answer)", CANNED_CONTEXT in ctx2)
    check("outage recall: names roughly when the snapshot was captured (a timestamp)",
          "UTC" in ctx2)
    check("outage recall: tells the reader this is not a live answer",
          "unreachable" in ctx2.lower() or "not a live answer" in ctx2.lower()
          or "not live" in ctx2.lower())

    # 3. outage but NO snapshot exists yet for this namespace: falls back to the
    # pre-existing (unchanged) "memory OFF" notice — never fabricates a stale answer.
    fresh_home = tempfile.mkdtemp(prefix="memnos_wb_stale_hook_fresh_")
    r3, out3 = hook_recall(fresh_home, dead_port_url(), "test:wb-stale-never-seen",
                           "anything", session_id="s3")
    check("outage + no prior snapshot: still exits 0", r3.returncode == 0)
    check("outage + no prior snapshot: no additionalContext fabricated",
          not (out3.get("hookSpecificOutput") or {}).get("additionalContext"))
    check("outage + no prior snapshot: falls back to the existing unreachable notice",
          "unreachable" in (out3.get("systemMessage") or "").lower())

    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(fresh_home, ignore_errors=True)


# ---- part 2: the MCP adapter's recall() tool -----------------------------------------

def test_mcp():
    print("=== MCP adapter: recall() serves a labeled-STALE snapshot on outage ===")
    srv, url = start_stub()
    home = tempfile.mkdtemp(prefix="memnos_wb_stale_mcp_")
    ns = "test:wb-stale-mcp"

    os.environ["HOME"] = home
    os.environ["MEMNOS_URL"] = url
    os.environ["MEMNOS_TOKEN"] = "mnk_test"
    os.environ["MEMNOS_NS"] = ns
    import memnos_mcp
    recall = getattr(memnos_mcp.recall, "fn", memnos_mcp.recall)

    live = recall("what coffee does Thameem like?")
    check("MCP live recall: contains the canned fact", CANNED_CONTEXT in live)
    check("MCP live recall: NOT labeled stale", "STALE" not in live)

    # point the SAME process at a dead server (no reload — HOME/NS are unchanged, only
    # the module-level URL constant needs updating for _post() to hit the dead port).
    memnos_mcp.URL = dead_port_url()
    stale = recall("what coffee does Thameem like?")
    check("MCP outage recall: does not raise / returns a string", isinstance(stale, str))
    check("MCP outage recall: explicitly labeled STALE", "STALE" in stale)
    check("MCP outage recall: still serves the cached fact from the SAME store",
          CANNED_CONTEXT in stale)
    check("MCP outage recall: names when the snapshot was captured",
          "UTC" in stale)

    srv.shutdown()
    shutil.rmtree(home, ignore_errors=True)


def main():
    test_hook()
    test_mcp()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
