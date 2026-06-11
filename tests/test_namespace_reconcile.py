"""`memnos namespace reconcile` — backfill for PRE-FIX contradiction debt (issue #10
residual C).

Namespaces ingested before the bf78b2e write-path fix hold contradicting LIVE facts the
fixed write path would have closed at write time. The reconcile verb walks the
namespace's live facts newest-first and applies the SAME deterministic write-time logic
(dedupe + SPO supersession + negation close-out via nearest stored embeddings) pairwise
against older live facts — embedding-only, NO LLM, no new embedding calls.

Covers: seeded debt -> dry-run counts correct AND writes nothing -> real run closes
them -> idempotent second run = 0 -> --limit bounds the walk. Exercised through the
REAL CLI (subprocess, noun-verb grammar) against a directly-seeded namespace; dry-run
and real-run counts must be identical by construction (same mutations, rolled back).

No server needed (direct-DB admin path, same _conn DSN trust as other namespace verbs).
"""
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:reconcile"
PY = sys.executable
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def cli(*args):
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "MEMNOS_DSN": DSN})
    return r.returncode, r.stdout + r.stderr


def counts(out):
    g = lambda k: int(re.search(rf"{k}\s+(\d+)", out).group(1))
    walked = g("facts walked")
    closed = g("would-close") if "would-close" in out else g("closed")
    deduped = g("would-dedupe") if "would-dedupe" in out else g("deduped")
    return walked, closed, deduped


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    # crafted vectors: negation pair at distance 0.20 (inside the 0.40 threshold),
    # everything else well separated; the dedupe pair shares one vector (distance 0).
    ANGLES = {
        "Project zeta is blocked by the database migration.": 0.0,
        "Project zeta is no longer blocked.": math.acos(1 - 0.20),
    }
    _auto = {}

    def emb(text):
        theta = ANGLES.get(text)
        if theta is None:
            theta = _auto.setdefault(text, 1.5 + 0.40 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    def reset():
        with store.conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    d1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 10, tzinfo=timezone.utc)

    def seed():
        """Known pre-fix debt: 1 SPO pair + 1 negation pair + 1 duplicate pair, all LIVE
        (the broken write path stored them without closing anything), plus an additive
        multi-valued pair that must survive untouched."""
        reset()
        ins = lambda stmt, subj, pred, obj, dt: store.insert_semantic(
            SCHEMA, NS, "fact", stmt, subject=subj, predicate=pred, obj=obj,
            valid_from=dt, vec=emb(stmt), observed_at=dt)
        ids = {}
        ids["rate_old"] = ins("The zeta API rate limit is 100 requests per second.",
                              "zeta API", "rate_limit", "100 requests per second", d1)
        ids["rate_new"] = ins("The zeta API rate limit was changed to 200 requests per second.",
                              "zeta API", "rate_limit", "200 requests per second", d2)
        ids["blocked"] = ins("Project zeta is blocked by the database migration.",
                             "Project zeta", "is_blocked_by", "database migration", d1)
        ids["negation"] = ins("Project zeta is no longer blocked.", "Project zeta", "", "", d2)
        ids["dup_old"] = ins("Zeta production runs in the us-east region.",
                             "Zeta production", "", "", d1)
        # identical restatement (distance 0) stored as a second live row pre-fix
        ids["dup_new"] = store.insert_semantic(
            SCHEMA, NS, "fact", "Zeta production runs in the us-east region.",
            subject="Zeta production", predicate="", obj="",
            valid_from=d2, vec=emb("Zeta production runs in the us-east region."),
            observed_at=d2)
        ids["paris"] = ins("Alice visited Paris.", "Alice", "visited", "Paris", d1)
        ids["rome"] = ins("Alice visited Rome.", "Alice", "visited", "Rome", d2)
        return ids

    def row(fid):
        with store.conn.cursor() as c:
            c.execute(f"SELECT valid_to, expired_at, superseded_by, restatements "
                      f"FROM {SCHEMA}.semantic WHERE id=%s", (fid,))
            return c.fetchone()

    def live_count():
        with store.conn.cursor() as c:
            c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s "
                      f"AND valid_to IS NULL AND expired_at IS NULL", (NS,))
            return c.fetchone()["n"]

    ids = seed()
    check("seed: 8 live facts (the pre-fix debt)", live_count() == 8)

    # --- 1. dry-run: correct counts, zero writes ----------------------------------------
    print("=== dry-run ===")
    rc, out = cli("namespace", "reconcile", NS, "--dry-run")
    check("dry-run exits 0", rc == 0)
    walked, closed, deduped = counts(out)
    check("dry-run reports would-close (not closed)", "would-close" in out and "would-dedupe" in out)
    check(f"dry-run: walked all 8 live facts (got {walked})", walked == 8)
    check(f"dry-run: would-close = 2 [SPO + negation] (got {closed})", closed == 2)
    check(f"dry-run: would-dedupe = 1 (got {deduped})", deduped == 1)
    check("dry-run wrote NOTHING (all 8 still live)", live_count() == 8)
    check("dry-run left no superseded_by stamps",
          row(ids["rate_old"])["superseded_by"] is None and row(ids["blocked"])["superseded_by"] is None)

    # --- 2. real run: closes the debt ----------------------------------------------------
    print("=== real run ===")
    rc, out = cli("namespace", "reconcile", NS)
    check("real run exits 0", rc == 0)
    walked, closed, deduped = counts(out)
    check(f"real run: closed = 2 (got {closed})", closed == 2)
    check(f"real run: deduped = 1 (got {deduped})", deduped == 1)
    r = row(ids["rate_old"])
    check("SPO debt: rate-limit 100 closed, linked to 200",
          r["valid_to"] is not None and r["superseded_by"] == ids["rate_new"])
    check("SPO debt: rate-limit 200 stays live", row(ids["rate_new"])["valid_to"] is None)
    r = row(ids["blocked"])
    check("negation debt: blocked fact closed, linked to the reversal",
          r["valid_to"] is not None and r["superseded_by"] == ids["negation"])
    check("dedupe debt: NEWER duplicate expired", row(ids["dup_new"])["expired_at"] is not None)
    r = row(ids["dup_old"])
    check("dedupe debt: OLDER row kept + reinforced",
          r["expired_at"] is None and r["valid_to"] is None and r["restatements"] == 1)
    check("multi-valued guard: visited Paris + Rome BOTH stay live",
          row(ids["paris"])["valid_to"] is None and row(ids["rome"])["valid_to"] is None)

    # --- 3. idempotency: second run = 0 ---------------------------------------------------
    print("=== idempotency ===")
    rc, out = cli("namespace", "reconcile", NS)
    walked, closed, deduped = counts(out)
    check(f"second run closes nothing (closed={closed}, deduped={deduped})",
          rc == 0 and closed == 0 and deduped == 0)

    # --- 4. --limit bounds the walk -------------------------------------------------------
    print("=== --limit ===")
    seed()
    rc, out = cli("namespace", "reconcile", NS, "--dry-run", "--limit", "1")
    walked, closed, deduped = counts(out)
    check(f"--limit 1 walks exactly 1 fact (got {walked})", rc == 0 and walked == 1)

    # --- 5. grammar guards -----------------------------------------------------------------
    rc, out = cli("namespace", "reconcile")
    check("missing namespace exits non-zero with usage", rc != 0 and "usage:" in out)

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
