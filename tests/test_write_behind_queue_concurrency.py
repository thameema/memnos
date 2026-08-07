"""issue #37 Layer 3 — unit test for offline_queue.drain()'s concurrency safety.

The hook's SessionStart drain (memnos_cli.py) and the MCP adapter's opportunistic drain
(memnos_mcp.py) now point at the SAME offline_queue/ directory and can legitimately race
(e.g. a Claude Code session's SessionStart hook firing right as the long-lived MCP
process completes its own successful write). drain() claims each item via an atomic
`os.rename` before POSTing specifically so two concurrent drainers can never both send
the same item — this test is the only thing that actually exercises that, rather than
just trusting the rename is atomic.

No server needed: a stub HTTP server counts POSTs per distinct `text`, two threads run
`offline_queue.drain()` against the SAME queue dir concurrently, and we assert every
seeded item was replayed EXACTLY once and the queue dir ends empty.

Run: python tests/test_write_behind_queue_concurrency.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import offline_queue

PASS = FAIL = 0
N_ITEMS = 40


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


_posts = []
_lock = threading.Lock()


class CountingStub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        # a tiny artificial delay widens the race window between the two drainer threads
        time.sleep(0.01)
        with _lock:
            _posts.append(body.get("text"))
        resp = json.dumps({"turn_id": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 0), CountingStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    config_dir = tempfile.mkdtemp(prefix="memnos_wb_concurrency_")
    seeded = [f"concurrent-drain-item-{i:03d}" for i in range(N_ITEMS)]
    for text in seeded:
        offline_queue.enqueue(config_dir, "test:wb-concurrency", text, "user")
    check(f"seeded {N_ITEMS} distinct queue files",
          len(os.listdir(offline_queue.queue_dir(config_dir))) == N_ITEMS)

    results = []

    def drainer():
        results.append(offline_queue.drain(config_dir, url, "mnk_test", timeout=5))

    t1 = threading.Thread(target=drainer)
    t2 = threading.Thread(target=drainer)
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    srv.shutdown()

    counts = Counter(_posts)
    duplicated = {t: n for t, n in counts.items() if n > 1}
    missing = [t for t in seeded if counts.get(t, 0) == 0]

    check("both concurrent drain() calls completed", not t1.is_alive() and not t2.is_alive())
    check("every seeded item was posted AT LEAST once", not missing)
    check("NO item was posted more than once (atomic claim prevented a double-drain)",
          not duplicated)
    check("total POSTs == total seeded items (no dupes, no drops)",
          len(_posts) == N_ITEMS)
    remaining = [f for f in os.listdir(offline_queue.queue_dir(config_dir)) if f.endswith(".json")]
    check("the queue directory is empty afterward (every item claimed by exactly one drainer)",
          not remaining)

    shutil.rmtree(config_dir, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
