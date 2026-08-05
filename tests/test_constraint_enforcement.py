"""`memnos constraint add|ls|rm` — issue #28 Part 1 (storage + CLI only; the PreToolUse
hook itself is Part 2, covered separately since it can't be triggered from a real Claude
Code session in this test environment).

Covers: --enforce advise (default) writes ONLY the pinned memory (issue #27's existing
path) and registers NO control-plane row; --enforce ask|block WITHOUT --tool is REJECTED
before anything is written (no half-applied state); --enforce ask|block WITH --tool writes
both the pinned memory AND a control-plane row; `constraint ls` lists/filters by namespace;
`constraint rm` soft-deletes (active=false) and is idempotent (second rm reports nothing to
remove, doesn't error).

Exercised through the REAL CLI (subprocess, noun-verb grammar) against directly-seeded
state. No server needed for the control-plane parts (direct-DB admin path); the pinned-
memory write goes through the live server's /remember endpoint.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from core.control import Control
import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:constraint_enforcement"
PY = sys.executable
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cli(*args):
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_URL": URL})
    return r.returncode, r.stdout + r.stderr


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    def reset():
        with conn.cursor() as c:
            c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
            c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))
            c.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace=%s", (NS,))

    def pinned_turn_count():
        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM tenant_memnos.raw_turns "
                      "WHERE namespace=%s AND memory_type='constraint'", (NS,))
            return c.fetchone()["n"]

    reset()

    # --- 1. --enforce advise (default): pinned memory only, NO control-plane row ------------
    print("=== --enforce advise (default) ===")
    rc, out = cli("constraint", "add", NS, "never deploy on Friday without sign-off")
    check("advise: exits 0", rc == 0)
    check("advise: pinned the memory", "constraint pinned" in out and pinned_turn_count() == 1)
    check("advise: no 'enforced:' line (nothing registered)", "enforced:" not in out)
    check("advise: no control-plane row written",
          len(Control.list_constraint_enforcement(conn, namespace=NS)) == 0)

    # --- 2. --enforce block WITHOUT --tool: rejected, NOTHING written ------------------------
    print("=== --enforce block, no --tool (must reject) ===")
    reset()
    rc, out = cli("constraint", "add", NS, "never force-push to main", "--enforce", "block")
    check("rejected: exits non-zero", rc != 0)
    check("rejected: explains --tool is required", "--tool" in out and "requires" in out)
    check("rejected: did NOT write the pinned memory either (no half-applied state)",
          pinned_turn_count() == 0)
    check("rejected: no control-plane row", len(Control.list_constraint_enforcement(conn, namespace=NS)) == 0)

    # --- 3. --enforce ask WITHOUT --tool: same rejection ------------------------------------
    rc, out = cli("constraint", "add", NS, "confirm before bulk delete", "--enforce", "ask")
    check("ask without --tool: also rejected", rc != 0 and "--tool" in out)

    # --- 4. --enforce block WITH --tool: pinned memory AND control-plane row -----------------
    print("=== --enforce block --tool (full path) ===")
    reset()
    rc, out = cli("constraint", "add", NS, "never rm -rf without confirmation",
                  "--enforce", "block", "--tool", "Bash(rm*)")
    check("block+tool: exits 0", rc == 0)
    check("block+tool: pinned the memory", pinned_turn_count() == 1)
    check("block+tool: prints the enforced id/level/tool", "enforced: id=" in out and "level=block" in out)
    rows = Control.list_constraint_enforcement(conn, namespace=NS)
    check("block+tool: exactly one control-plane row", len(rows) == 1)
    check("block+tool: row has the right level/matcher/rule",
          rows[0]["enforce_level"] == "block" and rows[0]["tool_matcher"] == "Bash(rm*)"
          and rows[0]["rule_text"] == "never rm -rf without confirmation")
    check("block+tool: row is active", rows[0]["active"] is True)

    # --- 5. constraint ls: namespace-filtered and unfiltered --------------------------------
    print("=== constraint ls ===")
    rc, out = cli("constraint", "ls", NS)
    check("ls <ns>: exits 0 and shows the row", rc == 0 and "Bash(rm*)" in out and "block" in out)
    rc, out = cli("constraint", "ls")
    check("ls (no ns): still includes the row (cross-namespace listing)", NS in out)

    # --- 5b. constraint ls warns when the PreToolUse cache doesn't cover this rule yet -------
    # (issue #28 field report: "added" != "enforced" until claude-setup + a new session).
    print("=== constraint ls staleness warning ===")
    sys.path.insert(0, ROOT)
    import memnos_cli
    cache_path = memnos_cli._enforce_cache_path(NS)
    if os.path.exists(cache_path):
        os.remove(cache_path)
    rc, out = cli("constraint", "ls", NS)
    check("ls warns when no PreToolUse cache exists at all",
          rc == 0 and "no PreToolUse cache yet" in out and "claude-setup" in out)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        import json as _json
        _json.dump({"namespace": NS, "rules": []}, f)   # cache exists but doesn't cover this rule
    rc, out = cli("constraint", "ls", NS)
    check("ls warns when the cache exists but is stale (missing this rule)",
          rc == 0 and "cache is stale" in out)
    with open(cache_path, "w") as f:
        _json.dump({"namespace": NS, "rules": [{"id": rows[0]["id"], "enforce_level": "block",
                                                 "tool_matcher": "Bash(rm*)", "rule_text": "x"}]}, f)
    rc, out = cli("constraint", "ls", NS)
    check("ls does NOT warn once the cache actually covers this rule",
          rc == 0 and "no PreToolUse cache" not in out and "cache is stale" not in out)
    os.remove(cache_path)

    # --- 6. constraint rm: soft-delete, idempotent on a second call -------------------------
    print("=== constraint rm ===")
    row_id = rows[0]["id"]
    rc, out = cli("constraint", "rm", str(row_id))
    check("rm: exits 0 and confirms deactivation", rc == 0 and "deactivated" in out)
    check("rm: no longer listed as active", len(Control.list_constraint_enforcement(conn, namespace=NS)) == 0)
    check("rm: row still exists but inactive (soft-delete, not hard DELETE)",
          len(Control.list_constraint_enforcement(conn, namespace=NS, active_only=False)) == 1)
    rc, out = cli("constraint", "rm", str(row_id))
    check("rm again: exits 0, reports nothing to remove (idempotent, not an error)",
          rc == 0 and "no active constraint" in out)

    # --- 7. Control-layer validation guard (defense in depth beyond the CLI pre-check) -------
    print("=== Control.add_constraint_enforcement direct validation ===")
    try:
        Control.add_constraint_enforcement(conn, NS, "x", "advise", "Bash(x*)")
        check("Control rejects enforce_level='advise' (not a valid control-plane level)", False)
    except ValueError:
        check("Control rejects enforce_level='advise' (not a valid control-plane level)", True)
    try:
        Control.add_constraint_enforcement(conn, NS, "x", "block", "")
        check("Control rejects an empty tool_matcher", False)
    except ValueError:
        check("Control rejects an empty tool_matcher", True)

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
