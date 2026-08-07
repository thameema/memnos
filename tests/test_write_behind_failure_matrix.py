"""issue #37 Layer 3 — TEST 3: an embed-time/adapter-time failure (not just
connection-refused) also queues via the MCP adapter, rather than falling through to any
local-file fallback — and PERMANENT failures still don't.

NEGATIVE FINDING (recorded here so a reviewer doesn't need to re-hunt for it): no code
in this repository writes memories to a parallel/local-file store today. Searched:
  grep -rniE "local.?file|separate store|divergent|save.?to.?local" --include="*.py" .
  grep -rn "offline_queue|OfflineQueue" --include="*.py" .   # only memnos_cli.py, pre-#37
  full read of memnos_mcp.py, sdk/memnos_sdk/client.py, integrations/hermes/__init__.py
The "local file fallback" the issue describes is the HARNESS improvising after a tool
error ("the tool call fails, and the session falls back to a divergent local file
store") — not a memnos code path. The actual thing being replaced is memnos_mcp.py's old
behavior: `remember()`/`memory_write()` raised `ToolError("... FAILED — NOT saved ...")`
on EVERY exception, including transient ones — the exact failure mode that pushes a
session toward inventing somewhere else to remember something. This test proves that
path is gone for the WHOLE transient class (connection-refused AND a 5xx simulating an
embed/adapter-time error), not bypassed in one hand-picked case — and that permanent
failures (401/403/400) still raise loud, per the pinned Bug 4 contract in
tests/test_mcp_write_errors.py.

Each scenario runs in its OWN fresh temp HOME so the adapter's own opportunistic drain
(triggered on every successful call, including at import) can never interact with an
earlier scenario's still-queued item.

Run: python tests/test_write_behind_failure_matrix.py
"""
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


_STATUS = {"code": 200, "body": {}}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        code = _STATUS["code"]
        body = json.dumps(_STATUS["body"]).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                          # bound-then-closed: guaranteed connection-refused
    return f"http://127.0.0.1:{port}"


def snapshot_tree(root):
    out = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            out.add(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def import_mcp(home, url, ns="test:wb-matrix"):
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


def run_fresh_home():
    home = tempfile.mkdtemp(prefix="memnos_wb_matrix_")
    return home, snapshot_tree(home)


# ---- transient scenarios: must queue + return success, never raise -------------------

def transient_scenario(label, url, tool="remember"):
    home, before = run_fresh_home()
    mod = import_mcp(home, url)
    fn = getattr(getattr(mod, tool), "fn", getattr(mod, tool))
    raised = False
    result = None
    try:
        result = fn(f"{label} distinctive fact")
    except Exception as e:
        raised = True
        result = str(e)
    after = snapshot_tree(home)
    new = after - before
    queue_prefix = os.path.join(".memnos", "offline_queue") + os.sep
    queue_files = {p for p in new if p.startswith(queue_prefix)}
    # nsresolve's own machine_id() writes `.memnos/machine_id` as incidental namespace-
    # resolution housekeeping (issue #20, pre-#37, unrelated to write-behind at all) — the
    # ONLY other path this scenario is allowed to touch.
    stray = new - queue_files - {os.path.join(".memnos", "machine_id")}
    check(f"[{tool}] {label}: does NOT raise (no false 'FAILED — NOT saved')", not raised)
    check(f"[{tool}] {label}: returns a success-shaped string mentioning it's queued",
          isinstance(result, str) and not raised and "queued" in result.lower())
    check(f"[{tool}] {label}: exactly one artifact queued under offline_queue/",
          len(queue_files) == 1)
    check(f"[{tool}] {label}: NOTHING appeared anywhere else (never a separate store)",
          not stray)
    shutil.rmtree(home, ignore_errors=True)


# ---- permanent scenarios: must still raise, must NEVER be queued (Bug 4 contract) ----

def permanent_scenario(code, label, url):
    home, before = run_fresh_home()
    _STATUS["code"] = code
    _STATUS["body"] = {"error": label}
    mod = import_mcp(home, url)
    remember = getattr(mod.remember, "fn", mod.remember)
    raised = False
    msg = ""
    try:
        remember(f"a write that should be permanently rejected ({code})")
    except Exception as e:
        raised = True
        msg = str(e)
    after = snapshot_tree(home)
    new = after - before - {os.path.join(".memnos", "machine_id")}   # nsresolve housekeeping, unrelated
    check(f"HTTP {code} ({label}): remember() STILL RAISES (Bug 4 contract preserved)", raised)
    check(f"HTTP {code}: error message says FAILED / NOT saved",
          raised and ("FAILED" in msg or "NOT saved" in msg))
    check(f"HTTP {code}: NOTHING was queued — a permanent failure must not silently "
          f"retry forever", not new)
    shutil.rmtree(home, ignore_errors=True)


def main():
    srv, url = start_stub()

    print("=== TRANSIENT failures: connection-refused ===")
    transient_scenario("connection-refused", dead_url(), tool="remember")
    transient_scenario("connection-refused (memory_write alias)", dead_url(), tool="memory_write")

    print("=== TRANSIENT failures: HTTP 500 (simulated embed/adapter-time error) ===")
    _STATUS["code"] = 500
    _STATUS["body"] = {"error": "internal error"}
    transient_scenario("HTTP 500 embed/adapter-time failure", url, tool="remember")
    transient_scenario("HTTP 500 embed/adapter-time failure (memory_write alias)", url, tool="memory_write")

    print("=== PERMANENT failures: must raise, must NEVER queue ===")
    for code, label in ((401, "unauthorized"), (403, "forbidden"), (400, "bad request")):
        permanent_scenario(code, label, url)

    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
