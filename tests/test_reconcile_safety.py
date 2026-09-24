"""`memnos namespace reconcile` execution safety (issue #155).

Exercises the REAL CLI (subprocesses, real signals, real concurrent Postgres sessions)
against namespaces seeded with ~1000 realistic facts carrying REAL local embeddings
(fastembed bge-small — memnos' own local model; no OpenAI):

  1. RESUME  — a run is SIGTERMed mid-walk (exits cleanly at a fact boundary, watermark
     saved), resumed, SIGKILLed mid-walk (uncommitted chunk lost, watermark intact),
     resumed again to completion. End state must be IDENTICAL, row for row, to an
     uninterrupted run over an identical copy, and the resumed runs must start from the
     watermark rather than from the newest fact.
  2. ADVISORY LOCK — while another session holds the namespace's reconcile lock the CLI
     exits 3 without touching anything; two CLI runs launched together: exactly one
     works, the other exits 3; the end state is still identical to the reference.
  3. LOCK CONTENTION, reconcile side — a "live writer" transaction holds row locks on
     the namespace; reconcile must give up each wait after lock_timeout (~2s), roll the
     chunk back, back off, and complete once the writer commits. The writer itself
     never waits.
  4. LOCK CONTENTION, live side — real MemnosMemory.remember() calls whose SPO
     supersession UPDATEs the very rows reconcile is closing, issued continuously while
     reconcile runs: none may wait longer than ~one chunk (reconcile commits every
     <=1s), and the final state has exactly one live value per subject.

Also asserts the reconcile per-fact LOGIC is unchanged by chunking: any chunk size
yields the same end state as a single chunk.

No server needed (direct-DB admin path, like test_namespace_reconcile.py).
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from core.reconcile import RECONCILE_LOCK_CLASS  # noqa: E402
from core.store import BrainStore  # noqa: E402
import reconcile_dataset as ds  # noqa: E402

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PREFIX = "test:rsafe-"
NS_REF, NS_A, NS_LOCK, NS_CONT, NS_LIVE, NS_CHUNK1 = (
    PREFIX + s for s in ("ref", "resume", "lock", "contend", "live", "chunk1"))
PY = sys.executable
N_FACTS = 1000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def cli_args(ns, *extra):
    return [PY, os.path.join(ROOT, "memnos_cli.py"), "namespace", "reconcile", ns, *extra]


def cli_env():
    return {**os.environ, "MEMNOS_DSN": DSN}


def cli(ns, *extra, timeout=300):
    r = subprocess.run(cli_args(ns, *extra), capture_output=True, text=True,
                       timeout=timeout, env=cli_env())
    return r.returncode, r.stdout + r.stderr


def popen(ns, *extra):
    return subprocess.Popen(cli_args(ns, *extra), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=cli_env())


def counts(out):
    g = lambda k: int(re.search(rf"{k}\s+(\d+)", out).group(1))
    return {"walked": g("facts walked"), "closed": g("closed"), "deduped": g("deduped")}


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    from core.control import Control
    Control.init(store.conn)             # what server boot does (remember() needs the registry)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if dim % 384:
        print(f"SKIP: tenant_memnos embedding dim {dim} is not a multiple of 384")
        sys.exit(0)
    tile = dim // 384                    # 384 (local mode) -> 1, 1536 -> 4 (distances unchanged)
    spec = ds.build_spec(N_FACTS, seed=4242)
    t0 = time.time()
    emb = ds.embed_texts([s[0] for s in spec])
    print(f"(embedded {len(emb)} texts locally in {time.time() - t0:.1f}s; dim={dim})")

    def runs_row(ns):
        with store.conn.cursor() as c:
            c.execute("SELECT * FROM memnos_control.namespace_reconcile_runs "
                      "WHERE tenant_schema=%s AND namespace=%s", (SCHEMA, ns))
            return c.fetchone()

    def reset(ns):
        with store.conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
            c.execute("SELECT to_regclass('memnos_control.namespace_reconcile_runs') AS t")
            if c.fetchone()["t"]:
                c.execute("DELETE FROM memnos_control.namespace_reconcile_runs "
                          "WHERE namespace=%s", (ns,))

    def seed(ns, copies=1):
        reset(ns)
        with psycopg.connect(DSN) as conn:
            for i in range(copies):          # extra copies restate the first (later obs)
                ds.seed_namespace(conn, SCHEMA, ns, spec, emb, vtype=store.vtype, tile=tile,
                                  t0=datetime(2026, 1, 1 + i, tzinfo=timezone.utc))

    def snap(ns):
        return ds.snapshot(store.conn, SCHEMA, ns)

    def wait_for(pred, timeout=60, every=0.05):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(every)
        return None

    def live_count(ns):
        with store.conn.cursor() as c:
            c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s "
                      f"AND valid_to IS NULL AND expired_at IS NULL", (ns,))
            return c.fetchone()["n"]

    try:
        # --- reference: one uninterrupted run ----------------------------------------
        print("=== reference run ===")
        seed(NS_REF)
        rc, out = cli(NS_REF, "--chunk-size", "50")
        check("reference run completes (exit 0, 'applied')", rc == 0 and "(applied)" in out, out)
        ref = snap(NS_REF)
        n_changed = sum(1 for v in ref.values() if v[0] or v[1])
        check(f"reference run actually reconciled debt ({n_changed} rows closed/expired)",
              n_changed > 100)
        row = runs_row(NS_REF)
        check("completed run stamps finished_at on its watermark row",
              row is not None and row["finished_at"] is not None)
        # chunking must not change per-fact outcomes: big (time-capped) pages == 50-fact chunks
        seed(NS_CHUNK1)
        rc, out = cli(NS_CHUNK1, "--chunk-size", "100000")
        check("chunking does not change the outcome (100000-fact pages == 50-fact chunks)",
              rc == 0 and snap(NS_CHUNK1) == ref)

        # --- 1. RESUME: SIGTERM, then SIGKILL, then resume to completion ----------------
        print("=== resume after SIGTERM / SIGKILL ===")
        seed(NS_A)
        p = popen(NS_A, "--chunk-size", "50", "--pause-ms", "250")
        got = wait_for(lambda: (runs_row(NS_A) or {}).get("chunks", 0) >= 3, timeout=120)
        check("run commits chunks progressively (watermark advanced to >=3 chunks)", bool(got))
        t_sig = time.time()
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=60)
        check(f"SIGTERM -> clean exit 0 within 5s (took {time.time() - t_sig:.2f}s)",
              p.returncode == 0 and time.time() - t_sig < 5, out)
        check("SIGTERM -> reports interrupted + resume hint", "interrupted" in out, out)
        wm1 = runs_row(NS_A)
        check("watermark saved: unfinished, cursor set",
              wm1 and wm1["finished_at"] is None and wm1["cursor_id"] is not None)
        partial = snap(NS_A)
        check("interrupted run left real, committed progress",
              sum(1 for v in partial.values() if v[0] or v[1]) > 0)

        p = popen(NS_A, "--chunk-size", "50")
        got = wait_for(lambda: (runs_row(NS_A) or {}).get("chunks", 0) >= wm1["chunks"] + 2,
                       timeout=120, every=0.01)
        p.kill()                                     # SIGKILL: no handler, no cleanup
        out, _ = p.communicate(timeout=30)
        wm2 = runs_row(NS_A)
        check("SIGKILLed run: watermark still unfinished and advanced past the SIGTERM one",
              wm2["finished_at"] is None and wm2["chunks"] > wm1["chunks"]
              and (wm2["cursor_observed_at"], wm2["cursor_id"])
              < (wm1["cursor_observed_at"], wm1["cursor_id"]))

        rc, out = cli(NS_A, "--chunk-size", "50")
        check("resumed run completes", rc == 0 and "(applied)" in out, out)
        check("resumed run starts AFTER the saved watermark (not from the newest fact)",
              "resumed after" in out and f"id={wm2['cursor_id']}" in out, out)
        walked = counts(out)["walked"]
        check(f"resumed run walked only the remainder ({walked} < {N_FACTS})",
              0 < walked < N_FACTS - 100)
        got = snap(NS_A)
        diff = [k for k in ref if ref[k] != got.get(k)]
        check(f"end state after SIGTERM+SIGKILL+resume IDENTICAL to uninterrupted run "
              f"({len(diff)} of {len(ref)} rows differ)", not diff)
        live_before = live_count(NS_A)
        rc, out = cli(NS_A)
        c2 = counts(out)
        # (a second pass is NOT necessarily a no-op — one newest-first pass is not a
        # fixpoint for chained negation/supersession, in the old code as much as the
        # new: measured 10 more closes on this data with the pre-#155 code too)
        check("after completion the next run starts FRESH (re-walks every live fact, no "
              "resume) and dedupes nothing more",
              rc == 0 and "resumed after" not in out and c2["deduped"] == 0
              # every live fact is walked, except older ones this same pass closes
              # before the walk reaches them (they drop out of later pages)
              and live_before - c2["closed"] <= c2["walked"] <= live_before, out)

        # --- 2. ADVISORY LOCK ---------------------------------------------------------
        print("=== advisory lock: no overlapping runs ===")
        seed(NS_LOCK)
        holder = psycopg.connect(DSN, autocommit=True)
        holder.execute("SELECT pg_advisory_lock(%s, hashtext(%s))",
                       (RECONCILE_LOCK_CLASS, f"{SCHEMA}:{NS_LOCK}"))
        rc, out = cli(NS_LOCK)
        check("lock held elsewhere -> exit 3 'already running', nothing done",
              rc == 3 and "already running" in out and live_count(NS_LOCK) == N_FACTS, out)
        rc, out = cli(NS_REF + "-other-ns-not-locked")
        check("the lock is per-namespace (a different namespace is not blocked)", rc == 0, out)
        holder.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))",
                       (RECONCILE_LOCK_CLASS, f"{SCHEMA}:{NS_LOCK}"))
        holder.close()

        p1 = popen(NS_LOCK, "--chunk-size", "50", "--pause-ms", "150")
        p2 = popen(NS_LOCK, "--chunk-size", "50", "--pause-ms", "150")
        o1, _ = p1.communicate(timeout=300)
        o2, _ = p2.communicate(timeout=300)
        rcs = sorted([p1.returncode, p2.returncode])
        loser = o1 if p1.returncode == 3 else o2
        check(f"two concurrent runs: exactly one runs, the other exits 3 (rcs={rcs})",
              rcs == [0, 3] and "already running" in loser, o1 + o2)
        check("end state after the race IDENTICAL to the reference", snap(NS_LOCK) == ref)

        # --- 3. CONTENTION: reconcile backs off, the live writer never waits -------------
        print("=== lock contention: reconcile backs off ===")
        seed(NS_CONT)
        writer = psycopg.connect(DSN, autocommit=False)
        writer.execute(f"SELECT id FROM {SCHEMA}.semantic WHERE namespace=%s FOR UPDATE",
                       (NS_CONT,))                    # a live txn holding every row lock
        t_start = time.time()
        p = popen(NS_CONT, "--chunk-size", "50")
        time.sleep(4.0)
        # while reconcile is being refused, the writer keeps working unimpeded
        t0 = time.time()
        writer.execute(f"UPDATE {SCHEMA}.semantic SET salience=salience "
                       f"WHERE namespace=%s", (NS_CONT,))
        w_ms = (time.time() - t0) * 1000
        with store.conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM pg_stat_activity "
                      "WHERE application_name='memnos-reconcile' AND wait_event_type='Lock' "
                      "AND now() - query_start > interval '3 seconds'")
            long_waits = c.fetchone()["n"]
        writer.commit()
        writer.close()
        t_release = time.time()
        out, _ = p.communicate(timeout=300)
        m = re.search(r"lock-timeout retries: (\d+)", out)
        retries = int(m.group(1)) if m else -1
        check(f"live writer never waited on reconcile ({w_ms:.0f}ms for its UPDATE)", w_ms < 500)
        check("reconcile never sat in a lock wait longer than lock_timeout", long_waits == 0)
        check(f"reconcile hit lock_timeout, rolled back and retried ({retries} retries)",
              retries >= 1 and "LockNotAvailable" in out, out)
        check(f"reconcile completed after the writer released "
              f"({time.time() - t_release:.1f}s after release, {time.time() - t_start:.1f}s total)",
              p.returncode == 0 and "(applied)" in out, out)
        check("end state under contention IDENTICAL to the reference", snap(NS_CONT) == ref)

        # --- 4. CONTENTION: real remember() calls stay fast while reconcile runs --------
        print("=== lock contention: live remember() is not blocked ===")
        from core.service import MemnosMemory
        from core.local_models import embed as local_embed
        seed(NS_LIVE, copies=3)              # 3000 facts: a longer walk to overlap with
        spo = sorted({(s[1], s[2]) for s in spec if s[2] in ("rate_limit", "runs_on",
                                                                 "lives_in", "works_at")})
        mem = MemnosMemory(BrainStore(DSN), lambda t: list(local_embed(t)) * tile, dim=dim,
                           extract_fn=lambda text, date: [_fact_for(text)])
        lat, errors = [], []
        done = threading.Event()

        def live_traffic():
            i = 0
            while not done.is_set():
                subj, pred = spo[i % len(spo)]
                text = _statement(subj, pred, i)
                t0 = time.time()
                try:
                    mem.remember(NS_LIVE, text,
                                 observed_at=datetime.now(timezone.utc) + timedelta(days=1))
                except Exception as e:                # noqa: BLE001
                    errors.append(repr(e))
                lat.append(time.time() - t0)
                i += 1

        p = popen(NS_LIVE, "--chunk-size", "200")
        wait_for(lambda: runs_row(NS_LIVE) is not None, timeout=60, every=0.01)
        th = threading.Thread(target=live_traffic)
        th.start()
        out, _ = p.communicate(timeout=300)
        done.set()
        th.join(timeout=120)
        check("reconcile completed while live writes ran", p.returncode == 0 and "(applied)" in out, out)
        check(f"{len(lat)} live remember() calls, none errored", lat and not errors, str(errors[:3]))
        worst = max(lat) if lat else 99
        check(f"no live remember() waited longer than ~one chunk (worst {worst * 1000:.0f}ms)",
              worst < 2.0)
        with store.conn.cursor() as c:
            c.execute(f"SELECT lower(subject_entity) AS s, lower(predicate) AS p, count(*) AS n "
                      f"FROM {SCHEMA}.semantic WHERE namespace=%s AND valid_to IS NULL "
                      f"AND expired_at IS NULL AND lower(predicate) IN "
                      f"('rate_limit','runs_on','lives_in','works_at') GROUP BY 1,2 "
                      f"HAVING count(*) > 1", (NS_LIVE,))
            dupes = c.fetchall()
        check("after concurrent reconcile + live writes: one live value per subject+predicate",
              not dupes, str(dupes[:3]))
    finally:
        for ns in (NS_REF, NS_A, NS_LOCK, NS_CONT, NS_LIVE, NS_CHUNK1,
                   NS_REF + "-other-ns-not-locked"):
            reset(ns)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


def _statement(subj, pred, i):
    return {"rate_limit": f"The {subj} rate limit is {9000 + i} requests per second.",
            "runs_on": f"The {subj} runs on host{900 + i}.",
            "lives_in": f"{subj} lives in Springfield {i}.",
            "works_at": f"{subj} works at Company {i}."}[pred]


def _fact_for(text):
    """Deterministic 'extraction' for the live-traffic writes (no LLM): the statement
    is built by _statement, so its SPO is known exactly."""
    for pred, marker in (("rate_limit", " rate limit is "), ("runs_on", " runs on "),
                         ("lives_in", " lives in "), ("works_at", " works at ")):
        if marker in text:
            head, obj = text.rstrip(".").split(marker, 1)
            subj = head[4:] if head.startswith("The ") else head
            return {"subject": subj, "predicate": pred, "object": obj, "statement": text}
    raise ValueError(text)


if __name__ == "__main__":
    main()
