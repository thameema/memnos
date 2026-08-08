"""`memnos hook status` — issue #32: surface 'N enforce rules loaded for <ns>' at
SessionStart, alongside the existing memory ON/OFF line.

Before this, a namespace with zero enforce rules loaded (never `claude-setup`'d since a
constraint was added, wrong resolved namespace, stale cache, ...) was silently invisible —
the enforcement gate could be protecting nothing and nothing said so. `constraint ls`
already warned about a stale/missing cache (issue #28 field report), but only for someone
who thought to run it.

Covers: the count is accurate for a namespace with N active enforce rules; it updates
(both up and down) as constraints are added/removed via the real control-plane API; a
soft-removed (inactive) rule no longer counts; a different namespace's rules never leak
into this one's count; singular/plural wording; the zero-rule case is still printed (not
suppressed) so a zero-load state stays visible rather than looking like "no such thing to
report".

Exercised through the REAL CLI (`memnos hook status`, subprocess, stdin piped exactly as
Claude Code's SessionStart would), against a real Postgres control plane. MEMNOS_URL points
at an unreachable port so the memory-server-health half of the status line (irrelevant
here) resolves fast and deterministically to OFF, matching the existing pattern in
test_hooks.py.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from core.control import Control
import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PY = sys.executable
NS = "test:hook_status_enforce_count"
NS_OTHER = "test:hook_status_enforce_count_other"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def hook_status(ns, home):
    """Drive the real `hook status` SessionStart entry point via subprocess, exactly as
    Claude Code invokes it (JSON on stdin, one JSON systemMessage on stdout). MEMNOS_URL
    points at a closed local port (port 1, privileged/unbound) so the server-health check
    fails fast and deterministically instead of depending on any running memnos server."""
    env = {**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_NS": ns, "MEMNOS_URL": "http://127.0.0.1:1",
           "MEMNOS_TOKEN": "mnk_test", "HOME": home}
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", "status"],
                       input=json.dumps({"source": "startup"}), capture_output=True, text=True,
                       timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    return body.get("systemMessage", "")


def enforce_count_in_message(msg):
    """Extract the integer count from a "N enforce rule(s) loaded for <ns>" segment, or
    None if the line isn't present at all (distinct from a real 0)."""
    m = re.search(r"(\d+) enforce rules? loaded for ([^\s·]+)", msg)
    return (int(m.group(1)), m.group(2)) if m else None


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    def reset():
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace IN (%s,%s)",
                      (NS, NS_OTHER))

    reset()
    home = tempfile.mkdtemp()

    # --- 1. zero rules: the line is still printed, not suppressed, showing 0 -----------------
    print("=== zero rules loaded: still visible, not suppressed ===")
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("zero-rule line present (not omitted)", got is not None, msg)
    if got:
        check("zero-rule count == 0", got[0] == 0, str(got))
        check("zero-rule line names the resolved namespace", got[1] == NS, str(got))
    check("zero-rule wording is singular-safe ('rules', count=0)", "0 enforce rules loaded" in msg, msg)

    # --- 2. add three active rules -> count becomes 3 -----------------------------------------
    print("=== three rules added: count == 3 ===")
    ids = [
        Control.add_constraint_enforcement(conn, NS, "never rm -rf without confirmation",
                                           "block", "Bash(rm*)"),
        Control.add_constraint_enforcement(conn, NS, "ask before writing to /etc",
                                           "ask", "Write(/etc/*)"),
        Control.add_constraint_enforcement(conn, NS, "ask before curl to external hosts",
                                           "ask", "Bash(curl*)"),
    ]
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("three rules: count == 3", got == (3, NS), str(got))

    # --- 3. a DIFFERENT namespace's rules never leak into this one's count --------------------
    print("=== isolation: another namespace's rules don't inflate this count ===")
    Control.add_constraint_enforcement(conn, NS_OTHER, "unrelated rule in a different ns",
                                       "block", "Bash(sudo*)")
    Control.add_constraint_enforcement(conn, NS_OTHER, "another unrelated rule",
                                       "ask", "Write")
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("other namespace's 2 rules don't leak into NS's count (still 3)", got == (3, NS), str(got))
    other_msg = hook_status(NS_OTHER, home)
    other_got = enforce_count_in_message(other_msg)
    check("NS_OTHER independently reports its own 2 rules", other_got == (2, NS_OTHER), str(other_got))

    # --- 4. remove one rule (soft-delete) -> count drops to 2, singular wording ---------------
    print("=== removal drops the count ===")
    removed = Control.remove_constraint_enforcement(conn, ids[0])
    check("removal reported success", removed)
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("after removing 1 of 3: count == 2", got == (2, NS), str(got))

    Control.remove_constraint_enforcement(conn, ids[1])
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("after removing 2 of 3: count == 1", got == (1, NS), str(got))
    check("singular wording for count == 1 ('1 enforce rule loaded', not 'rules')",
          "1 enforce rule loaded" in msg and "1 enforce rules loaded" not in msg, msg)

    # --- 5. remove the last active rule -> back to 0, still visible ---------------------------
    print("=== back to zero after removing the last rule ===")
    Control.remove_constraint_enforcement(conn, ids[2])
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("all removed: count == 0 again", got == (0, NS), str(got))

    # --- 6. a re-added (new) rule is picked back up ---------------------------------------------
    print("=== re-adding a rule after zero is picked back up ===")
    Control.add_constraint_enforcement(conn, NS, "back again", "block", "Bash(kill*)")
    msg = hook_status(NS, home)
    got = enforce_count_in_message(msg)
    check("re-added rule: count == 1", got == (1, NS), str(got))

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
