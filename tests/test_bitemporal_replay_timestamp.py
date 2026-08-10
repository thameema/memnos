"""Bi-temporal replay timestamp regression (issue #42, critical).

`_write_fact` (core/service.py) keys belief-change supersession on the OBSERVATION
axis (`observed_at`) — see its docstring. Before this fix, a write-behind item
replayed by offline_queue.drain() had no mechanism at all to preserve when it
actually happened, so it landed with whatever time the replay POST happened to
receive it. The naive "fix" — have the replaying client pass its own captured
enqueue time through as observed_at — was rejected: a client that can supply its
own observation timestamp can backdate (or future-date) a stale queued write to
win a supersession over a fact genuinely written by someone else in the meantime.

The chosen fix instead makes observed_at NEVER client-suppliable, on ANY /remember
call (including a replayed one) — the server always stamps its own clock at the
moment it receives and commits the write. This file proves both halves:

  1. A write queued during a simulated outage and replayed later is stamped with
     the REPLAY-COMMIT time, not the (much older) original enqueue time.
  2. A client — replaying or not — that supplies its own observed_at/known_at in
     the request body is ignored/overridden, never honored. (Nothing downstream
     ever read this field even before the fix — the guard makes that explicit
     rather than incidental, and pins it against ever being wired through later.)

Needs a live server + Postgres (see reference in memory for this laptop's
env gotchas around running this outside CI):
    MEMNOS_DSN=postgresql://memnos:memnos_ci@localhost:5545/memnos \\
    MEMNOS_URL=http://127.0.0.1:8979 \\
    python tests/test_bitemporal_replay_timestamp.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore
import offline_queue

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
NS = "test:bitemporal-replay"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def call(path, token=None, body=None):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _observed_at_by_id(conn, turn_id):
    with conn.cursor() as c:
        c.execute(f"SELECT observed_at FROM {SCHEMA}.raw_turns WHERE id=%s", (turn_id,))
        row = c.fetchone()
    return row["observed_at"] if row else None


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    BrainStore(conn=conn).create_schema("memnos")
    cleanup(conn)

    pid = Control.create_principal(conn, "bitemporal-replay-tester", "user")
    token = Control.mint_token(conn, pid, "t")
    Control.grant(conn, pid, NS, can_read=True, can_write=True)

    config_dir = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"memnos_wb_bitemporal_{os.getpid()}")

    print("=== 1. replayed write is stamped with replay-commit time, not enqueue time ===")
    # Plain natural-language text — a hyphenated word+pid token trips the server's
    # high-entropy secret redaction, which would replace the stored text and break an
    # exact-text lookup. NS was just cleared above, so the row this write produces is
    # unambiguously identifiable as "the newest row in NS" without needing to match text.
    text1 = "the office queue was stuck during the network outage this morning"
    offline_queue.enqueue(config_dir, NS, text1, "user", token=token)

    # Simulate a long outage: back-date the queue item's own queued_at by 6 hours.
    # If the server (wrongly) trusted an item-carried timestamp for observed_at, this
    # backdate would show up in the stored row; it must not.
    qfiles = [f for f in os.listdir(offline_queue.queue_dir(config_dir)) if f.endswith(".json")]
    check("exactly one item queued", len(qfiles) == 1)
    qpath = os.path.join(offline_queue.queue_dir(config_dir), qfiles[0])
    with open(qpath) as fh:
        item = json.load(fh)
    old_queued_at = time.time() - 6 * 3600
    item["queued_at"] = old_queued_at
    with open(qpath, "w") as fh:
        json.dump(item, fh)

    t_before_replay = datetime.now(timezone.utc)
    drained, rejected = offline_queue.drain(config_dir, URL, token, timeout=15)
    t_after_replay = datetime.now(timezone.utc)
    check("drain() replayed exactly 1 item, 0 rejected", drained == 1 and rejected == 0)

    with conn.cursor() as c:
        c.execute(f"SELECT observed_at FROM {SCHEMA}.raw_turns "
                  f"WHERE namespace=%s ORDER BY id DESC LIMIT 1", (NS,))
        row = c.fetchone()
    obs1 = row["observed_at"] if row else None
    check("replayed write landed in the store", obs1 is not None)
    if obs1 is not None:
        check("observed_at falls within the drain() call window (replay-commit time)",
              t_before_replay - timedelta(seconds=5) <= obs1 <= t_after_replay + timedelta(seconds=5))
        gap_from_old_queued_at = abs((obs1 - datetime.fromtimestamp(old_queued_at, tz=timezone.utc))
                                      .total_seconds())
        check("observed_at is NOT anywhere near the (6h-old) original enqueue time",
              gap_from_old_queued_at > 3000)

    print("=== 2. a client-supplied observed_at/known_at in the request body is ignored ===")
    text2 = "someone insists this note was written a very long time ago"
    forged = "2001-09-09T01:46:40+00:00"          # nowhere near "now"
    t_before_direct = datetime.now(timezone.utc)
    # A real replaying client (offline_queue._post_remember) never sends this field —
    # this simulates a hand-crafted queue item / a buggy or adversarial client trying
    # to sneak one in directly at the HTTP boundary the replay path also uses.
    s, j = call("/remember", token, {"namespace": NS, "text": text2, "speaker": "user",
                                      "async": True, "observed_at": forged,
                                      "known_at": forged})
    t_after_direct = datetime.now(timezone.utc)
    check("forged-timestamp write still succeeds (200)", s == 200)

    obs2 = _observed_at_by_id(conn, j.get("turn_id")) if j.get("turn_id") else None
    check("forged write landed in the store", obs2 is not None)
    if obs2 is not None:
        check("observed_at reflects the server's OWN receipt time, not the forged value",
              t_before_direct - timedelta(seconds=5) <= obs2 <= t_after_direct + timedelta(seconds=5))
        gap_from_forged = abs((obs2 - datetime.fromisoformat(forged)).total_seconds())
        check("the forged 2001 timestamp had zero effect on the stored observed_at",
              gap_from_forged > 3600)

    cleanup(conn)
    import shutil
    shutil.rmtree(config_dir, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
