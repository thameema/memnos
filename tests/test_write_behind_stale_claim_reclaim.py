"""issue #37 Layer 3 / adversarial-review finding #1 — offline_queue.drain()'s crash
recovery for a claim orphaned mid-drain.

drain() claims an item via `os.rename` to `<name>.json.claiming-<pid>` BEFORE POSTing
it, and only resolves that claim (removes it on success, renames it back on a
transient failure, or `.rejected`s it on a permanent one) once the POST returns. If the
process is killed in between — the exact failure mode issue #37 exists to survive — the
claim file is invisible to drain()'s `*.json` scan forever: silent, permanent data
loss, no error, and drain() returns (0, 0) as if there was nothing to do.

Fix: drain() sweeps for `.claiming-*` files that are STALE by mtime age (not by pid
liveness — a live process legitimately mid-POST must never have its claim stolen,
which would double-send) and reclaims them back into the normal `*.json` scan.

This test proves BOTH directions:
  1. An OLD orphaned claim (simulating a crash) IS reclaimed and successfully replayed.
  2. A FRESH claim (simulating a live in-flight POST) is NOT reclaimed/double-sent.

No server needed beyond a stub that records POST bodies by text.

Run: python tests/test_write_behind_stale_claim_reclaim.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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


_posts = []
_lock = threading.Lock()


class CountingStub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
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


def start_stub():
    srv = HTTPServer(("127.0.0.1", 0), CountingStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def _seed_claimed_item(config_dir, text, age_seconds):
    """Enqueue a real item via the public API, then manually perform the same claim
    rename drain() itself does — and stop there, exactly as if the drainer had been
    killed the instant after claiming but before the POST resolved. `age_seconds`
    controls how old the claim file's mtime is made, simulating either a stale
    (crashed) claim or a fresh (live, mid-POST) one."""
    path = offline_queue.enqueue(config_dir, "test:wb-reclaim", text, "user")
    claimed = path + ".claiming-999999"          # an arbitrary pid — must not matter, see above
    os.rename(path, claimed)
    stamp = time.time() - age_seconds
    os.utime(claimed, (stamp, stamp))
    return claimed


def test_old_orphaned_claim_is_reclaimed_and_replayed(url):
    print("=== old orphaned claim (simulated crash) IS reclaimed and replayed ===")
    config_dir = tempfile.mkdtemp(prefix="memnos_wb_reclaim_old_")
    claimed = _seed_claimed_item(config_dir, "orphaned-crash-item",
                                  age_seconds=offline_queue.STALE_CLAIM_AGE + 30)
    del _posts[:]

    drained, rejected = offline_queue.drain(config_dir, url, "mnk_test", timeout=5)

    check("drain() reclaimed and replayed the orphaned item (drained == 1)", drained == 1)
    check("nothing was permanently rejected", rejected == 0)
    check("the item was actually POSTed to the server (not silently dropped)",
          _posts == ["orphaned-crash-item"])
    check("the stale claim file is gone", not os.path.exists(claimed))
    remaining = os.listdir(offline_queue.queue_dir(config_dir))
    check("the queue directory is empty afterward", not remaining)

    shutil.rmtree(config_dir, ignore_errors=True)


def test_fresh_claim_is_not_stolen_or_double_sent(url):
    print("=== fresh claim (simulated live in-flight POST) is NOT reclaimed/double-sent ===")
    config_dir = tempfile.mkdtemp(prefix="memnos_wb_reclaim_fresh_")
    claimed = _seed_claimed_item(config_dir, "live-in-flight-item", age_seconds=1)
    del _posts[:]

    drained, rejected = offline_queue.drain(config_dir, url, "mnk_test", timeout=5)

    check("drain() did NOT touch the fresh claim (drained == 0)", drained == 0)
    check("nothing was rejected", rejected == 0)
    check("the item was NOT posted — a live claim must never be stolen/double-sent",
          _posts == [])
    check("the fresh claim file is untouched, still under its claimed name",
          os.path.exists(claimed))
    remaining = [f for f in os.listdir(offline_queue.queue_dir(config_dir)) if f.endswith(".json")]
    check("no plain .json file appeared (it was not reclaimed back)", not remaining)

    shutil.rmtree(config_dir, ignore_errors=True)


def main():
    srv, url = start_stub()
    test_old_orphaned_claim_is_reclaimed_and_replayed(url)
    test_fresh_claim_is_not_stolen_or_double_sent(url)
    srv.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
