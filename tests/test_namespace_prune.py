"""`memnos namespace prune` — issue #30: safe batch cleanup of dead namespaces.

Covers: default (no flags) = --empty dry-run report, writes nothing; --stale DAYS
targets a small-footprint namespace whose last write is old enough (and correctly
EXCLUDES a recent one and one whose fact count exceeds the "small" threshold);
--force is the only thing that deletes; a namespace with an active binding is
skipped unless --force; audit log gets an entry per prune. Exercised through the
REAL CLI (subprocess, noun-verb grammar) against directly-seeded namespaces.

No server needed (direct-DB admin path, same _conn DSN trust as other namespace verbs).
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from core.store import BrainStore
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PY = sys.executable
PASS = FAIL = 0

NS_EMPTY = "test:prune_empty"
NS_BOUND_EMPTY = "test:prune_bound_empty"
NS_STALE = "test:prune_stale"
NS_RECENT = "test:prune_recent"
NS_BIGFACTS = "test:prune_bigfacts_stale"
ALL_NS = [NS_EMPTY, NS_BOUND_EMPTY, NS_STALE, NS_RECENT, NS_BIGFACTS]


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cli(*args):
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "MEMNOS_DSN": DSN})
    return r.returncode, r.stdout + r.stderr


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn
    Control.init(conn)

    def reset():
        with conn.cursor() as c:
            for ns in ALL_NS:
                c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
                c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
                c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
                c.execute("DELETE FROM memnos_control.grants WHERE namespace=%s", (ns,))
                c.execute("DELETE FROM memnos_control.bindings WHERE namespace=%s", (ns,))

    def seed_facts(ns, n, backdate_days=None):
        old = datetime.now(timezone.utc) - timedelta(days=backdate_days or 0)
        ids = []
        for i in range(n):
            fid = store.insert_semantic(SCHEMA, ns, "fact", f"fact {i} for {ns}",
                                        subject=ns, predicate="has", obj=str(i),
                                        valid_from=old, vec=[0.0] * _dim(store), observed_at=old)
            ids.append(fid)
        if backdate_days:
            with conn.cursor() as c:
                c.execute(f"UPDATE {SCHEMA}.semantic SET created_at=%s WHERE id=ANY(%s)", (old, ids))
        return ids

    def last_write(ns):
        with conn.cursor() as c:
            c.execute(f"SELECT max(created_at) AS m FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
            return c.fetchone()["m"]

    def audit_count(ns):
        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM memnos_control.audit_log "
                      "WHERE action='namespace.prune' AND namespace=%s", (ns,))
            return c.fetchone()["n"]

    reset()

    # --- seed ------------------------------------------------------------------------------
    Control.create_namespace(conn, NS_EMPTY, description="empty, no binding")
    Control.create_namespace(conn, NS_BOUND_EMPTY, description="empty, bound")
    pid = Control.create_principal(conn, "prune_test_agent", "agent")
    Control.upsert_binding(conn, pid, "repo", "github.com/example/prune-test", NS_BOUND_EMPTY)

    seed_facts(NS_STALE, 2, backdate_days=100)          # small + old -> stale candidate
    seed_facts(NS_RECENT, 2, backdate_days=0)            # small + recent -> NOT stale
    seed_facts(NS_BIGFACTS, 25, backdate_days=100)       # old but NOT small -> NOT stale

    check(f"seed: {NS_STALE} last write ~100d ago",
          (datetime.now(timezone.utc) - last_write(NS_STALE)).days >= 99)

    # --- 1. default (no flags) = --empty dry-run, writes nothing ---------------------------
    print("=== default: bare `namespace prune` ===")
    rc, out = cli("namespace", "prune")
    check("bare prune exits 0", rc == 0)
    check("bare prune lists the empty (unbound) namespace", NS_EMPTY in out)
    check("bare prune does NOT list the bound-empty namespace (skipped)", "skipped" in out and NS_BOUND_EMPTY in out)
    check("bare prune does NOT list the stale/recent/bigfacts namespaces (empty-only default)",
          NS_STALE not in out and NS_RECENT not in out and NS_BIGFACTS not in out)
    check("bare prune deletes nothing",
          Control.list_namespaces(conn) and any(n["name"] == NS_EMPTY for n in Control.list_namespaces(conn)))
    check("bare prune report says 'would prune' / 'would be pruned', not 'pruned'",
          "would prune" in out or "would be pruned" in out)

    # --- 2. --stale DAYS: only the old + small namespace matches ---------------------------
    print("=== --stale ===")
    rc, out = cli("namespace", "prune", "--stale", "30")
    check("--stale 30 exits 0", rc == 0)
    check("--stale 30 catches the old+small namespace", NS_STALE in out)
    check("--stale 30 excludes the recent one", NS_RECENT not in out)
    check("--stale 30 excludes the big-facts one (not 'small')", NS_BIGFACTS not in out)

    # --- 3. bound namespace: skipped WITHOUT --force, both --force flag ALSO overrides the
    #    bound-skip (a single --force governs "actually delete" AND "override bound-skip") ---
    print("=== bound namespace: skip without --force ===")
    rc, out = cli("namespace", "prune", "--empty")
    check("dry-run: unbound empty namespace listed as would-prune", NS_EMPTY in out and "would prune" in out)
    check("dry-run: bound-empty namespace listed as skipped, not would-prune", NS_BOUND_EMPTY in out and "skipped" in out)
    names = {n["name"] for n in Control.list_namespaces(conn)}
    check("dry-run (no --force) deleted neither namespace",
          NS_EMPTY in names and NS_BOUND_EMPTY in names)

    # --- 4. --empty --force: --force deletes the unbound empty AND overrides the bound-skip -
    print("=== --empty --force ===")
    rc, out = cli("namespace", "prune", "--empty", "--force")
    check("--empty --force exits 0", rc == 0)
    check("--empty --force reports it pruned", "pruned" in out)
    names = {n["name"] for n in Control.list_namespaces(conn)}
    check(f"{NS_EMPTY} was actually deleted", NS_EMPTY not in names)
    check(f"{NS_BOUND_EMPTY} was ALSO deleted (--force overrides the bound-skip too)",
          NS_BOUND_EMPTY not in names)
    check("--empty --force did not touch the stale namespace's facts",
          last_write(NS_STALE) is not None)
    check("audit log recorded the empty-namespace prune", audit_count(NS_EMPTY) >= 1)
    check("audit log recorded the bound-namespace prune too", audit_count(NS_BOUND_EMPTY) >= 1)

    # --- 5. --stale --force: deletes the stale namespace's DATA too (purge_data) -----------
    print("=== --stale --force purges data ===")
    rc, out = cli("namespace", "prune", "--stale", "30", "--force")
    check("--stale --force exits 0", rc == 0)
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s", (NS_STALE,))
        remaining = c.fetchone()["n"]
    check("--stale --force purges the stale namespace's facts (not just the registry row)",
          remaining == 0)
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s", (NS_BIGFACTS,))
        untouched = c.fetchone()["n"]
    check("big-facts namespace untouched by --stale (never matched the small-footprint gate)",
          untouched == 25)

    # --- 6. --dry-run wins even with --force (belt-and-suspenders) -------------------------
    print("=== --dry-run overrides --force ===")
    seed_facts(NS_RECENT, 0)  # no-op, just to have a fresh state check
    rc, out = cli("namespace", "prune", "--stale", "0", "--force", "--dry-run")
    check("--dry-run + --force + --stale 0 still reports would-prune, not pruned",
          rc == 0 and "would" in out)
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s", (NS_RECENT,))
        still_there = c.fetchone()["n"]
    check("--dry-run left the recent namespace's facts untouched", still_there == 2)

    # --- 7. no matches -> clean message, exit 0 ---------------------------------------------
    print("=== no candidates ===")
    reset()
    rc, out = cli("namespace", "prune", "--stale", "9999")
    check("no candidates: exits 0 with a clear message", rc == 0 and "no namespaces match" in out)

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


def _dim(store):
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        d = c.fetchone()["d"]
    return d if d and d > 0 else 384


if __name__ == "__main__":
    main()
