"""issue #45 — no-regression proof for the stdio transport (the normal Claude Code /
agent-setup path, which has a real per-process MEMNOS_TOKEN). The fix makes drain()
prefer each item's OWN captured token over the shared fallback; this test proves that
change is a no-op for stdio, where the captured token and the fallback are always the
same value:

  - a LEGACY item with no captured token at all (the on-disk shape every queued item had
    before this fix — offline_queue.enqueue()'s new `token=` kwarg defaults to "", so any
    caller that doesn't pass it, and any queue file already on disk from before this
    change, produces exactly this shape) must still drain successfully off the module-
    level TOKEN fallback, unchanged from before.
  - a NEW-shape item carrying its own captured token (what memnos_mcp.remember()'s
    exception handler now always writes via token=_token()) must ALSO drain successfully
    — on stdio _token() always returns the same module-level TOKEN, so this is the same
    outcome via the other code path.

Drives memnos_mcp._drain_offline_queue() directly (the exact function named in issue
#45) against a stub HTTP server, so this stays fast (no real Postgres/embeddings needed)
while still exercising the real function under test rather than reimplementing its logic.

Run: python tests/test_write_behind_stdio_token_regression.py
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
STDIO_TOKEN = "mnk_stdio_regress_test"
NS = "test:wb-stdio-token-regress"
TEXT_LEGACY = "wb-stdio-token-regress LEGACY (no captured token) FACT: the chess olympiad moved to Chennai."
TEXT_CAPTURED = "wb-stdio-token-regress CAPTURED-TOKEN FACT: the film festival moved to Busan."

received_auth = []          # Authorization headers the stub actually saw, for extra rigor


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        received_auth.append(self.headers.get("Authorization", ""))
        if self.headers.get("Authorization") == f"Bearer {STDIO_TOKEN}":
            code, resp_body = 200, {"turn_id": 1, "facts": 0, "namespace": NS}
        else:
            code, resp_body = 401, {"error": "unauthorized"}
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


def import_mcp(home, url):
    os.environ["HOME"] = home
    os.environ["MEMNOS_URL"] = url
    os.environ["MEMNOS_TOKEN"] = STDIO_TOKEN
    os.environ["MEMNOS_NS"] = NS
    if "memnos_mcp" in sys.modules:
        import memnos_mcp
        importlib.reload(memnos_mcp)
    else:
        import memnos_mcp
    return sys.modules["memnos_mcp"]


def main():
    srv, url = start_stub()
    home = tempfile.mkdtemp(prefix="memnos_wb_stdio_token_")
    mod = import_mcp(home, url)          # fresh import — startup drain sees an empty queue
    check("stdio adapter resolves TOKEN from MEMNOS_TOKEN as expected", mod.TOKEN == STDIO_TOKEN)

    cfg_dir = mod._config_dir()
    # Seeded AFTER import (bypassing the adapter) so these are discovered only by the
    # drain call below, not the startup drain.
    offline_queue.enqueue(cfg_dir, NS, TEXT_LEGACY, "user")                       # no token=
    offline_queue.enqueue(cfg_dir, NS, TEXT_CAPTURED, "user", token=STDIO_TOKEN)  # captured

    qdir = offline_queue.queue_dir(cfg_dir)
    check("both items seeded", len(os.listdir(qdir)) == 2, str(os.listdir(qdir)))

    drained, rejected = mod._drain_offline_queue()
    check("BOTH the legacy (tokenless) item and the newly-captured-token item drain "
          "successfully via the stdio TOKEN fallback — zero regression for the normal "
          "Claude Code / agent-setup usage this issue explicitly must not break",
          drained == 2 and rejected == 0, f"drained={drained} rejected={rejected}")

    check("the queue directory is empty after a clean drain (nothing left behind)",
          os.listdir(qdir) == [])

    check("every drained POST actually carried the real per-process bearer token "
          "(not an empty/missing Authorization header)",
          received_auth == [f"Bearer {STDIO_TOKEN}"] * 2, str(received_auth))

    shutil.rmtree(home, ignore_errors=True)
    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
