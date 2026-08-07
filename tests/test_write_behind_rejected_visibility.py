"""issue #37 Layer 3 / adversarial-review finding #2 — a permanently-rejected queued item
must be VISIBLE to an MCP-only caller (Claude Desktop, omnigent), not just to the CLI hook.

Before the fix, `memnos_mcp.py`'s `_drain_offline_queue()` discarded `offline_queue.drain()`'s
`(drained, rejected)` return value entirely — a `.rejected` item (e.g. a revoked token
discovered at replay time) was written to disk and never surfaced anywhere: no tool
response, no warning, nothing. `memnos_cli.py`'s hook path already folded a rejected count
into its one-line SessionStart status (`⚠ N queued turns rejected`); MCP-only hosts never
run that hook, so they had zero equivalent.

This test proves the fix reaches an actual TOOL RESPONSE, not just that nothing crashes:
a queued item is seeded directly (bypassing the adapter) so it's ONLY discovered by the
opportunistic drain a later successful `remember()` call triggers — not by the drain at
import time, which would only be visible via the stderr log, never in anything the MCP
caller can read. The stub differentiates by POSTed text: the new write's own text always
succeeds (200); the seeded item's text is permanently rejected (401) — so one `remember()`
call both succeeds AND discovers the older rejection in the same opportunistic drain.

Run: python tests/test_write_behind_rejected_visibility.py
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import offline_queue

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


_REJECT_TEXTS = set()          # POSTed texts the stub should permanently reject (401)


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("text") in _REJECT_TEXTS:
            code, resp_body = 401, {"error": "unauthorized (revoked token)"}
        else:
            code, resp_body = 200, {"turn_id": 1, "facts": 0, "namespace": "test:wb-rejvis"}
        resp = json.dumps(resp_body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def start_stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def import_mcp(home, url, ns="test:wb-rejvis"):
    os.environ["HOME"] = home
    os.environ["MEMNOS_URL"] = url
    os.environ["MEMNOS_TOKEN"] = "mnk_test"
    os.environ["MEMNOS_NS"] = ns
    if "memnos_mcp" in sys.modules:
        import memnos_mcp
        importlib.reload(memnos_mcp)
    else:
        import memnos_mcp
    return sys.modules["memnos_mcp"]


def main():
    srv, url = start_stub()

    home = tempfile.mkdtemp(prefix="memnos_wb_rejvis_")
    mod = import_mcp(home, url)                  # fresh import — startup drain sees an empty queue
    remember = getattr(mod.remember, "fn", mod.remember)
    memory_write = getattr(mod.memory_write, "fn", mod.memory_write)

    # Seed a queue item AFTER import (bypassing the adapter) so it's discovered only by a
    # LATER call's opportunistic drain — not the startup drain, whose only signal is stderr.
    doomed_text = "wb-rejvis doomed queued item (revoked token)"
    _REJECT_TEXTS.add(doomed_text)
    offline_queue.enqueue(os.path.join(home, ".memnos"), "test:wb-rejvis", doomed_text, "user")

    result = remember("wb-rejvis live write that succeeds normally")
    check("remember() still succeeds normally even though an unrelated queued item "
          "is rejected during its opportunistic drain",
          isinstance(result, str) and "remembered" in result.lower())
    check("the rejected count is folded into remember()'s OWN tool response text "
          "(not just logged where the caller can't see it)",
          "previously-queued" in result and "1 previously-queued" in result)

    shutil.rmtree(home, ignore_errors=True)
    _REJECT_TEXTS.discard(doomed_text)

    # Same proof for memory_write() (the /memory/write alias) — separate fresh home so its
    # own startup drain doesn't collide with the scenario above.
    home2 = tempfile.mkdtemp(prefix="memnos_wb_rejvis2_")
    mod2 = import_mcp(home2, url)
    memory_write = getattr(mod2.memory_write, "fn", mod2.memory_write)
    doomed_text2 = "wb-rejvis doomed queued item #2 (revoked token)"
    _REJECT_TEXTS.add(doomed_text2)
    offline_queue.enqueue(os.path.join(home2, ".memnos"), "test:wb-rejvis", doomed_text2, "user")

    result2 = memory_write("wb-rejvis live memory_write that succeeds normally")
    check("memory_write() still succeeds normally despite the unrelated rejection",
          isinstance(result2, str) and "written" in result2.lower())
    check("memory_write() also folds the rejected count into its OWN tool response text",
          "previously-queued" in result2 and "1 previously-queued" in result2)

    shutil.rmtree(home2, ignore_errors=True)
    _REJECT_TEXTS.discard(doomed_text2)

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
