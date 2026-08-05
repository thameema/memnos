"""`memnos hook enforce` (PreToolUse) + `_refresh_enforce_cache` (SessionStart) — issue #28
part 2.

What this CAN verify (and does): the cache-refresh write, the hook's stdin-in/stdout-out
contract for match/no-match/no-cache cases, the exact hookSpecificOutput.permissionDecision
JSON shape, block-beats-ask precedence, the --tool glob matching against BOTH the rich
Claude-Code-style subject (Bash(cmd)) and the bare tool name, a broken matcher failing OPEN
for itself only (not suppressing a different rule's real match, not blocking everything),
and every matched decision getting audit-logged.

What this CANNOT verify (documented, not silently assumed): that Claude Code's OWN
PreToolUse mechanism actually invokes this hook, that the "*" matcher used to register it
in settings.json actually means "every tool" (seen in use by another pre-existing hook on
the dev machine, which is stronger evidence than doc inference alone, but still not
independently confirmed from inside memnos), or that a real session's permission UI honors
"ask"/"deny" as expected. Those need a real Claude Code session — see the #28 issue thread
for the pending validation checklist. (See test_agent_setup.py for coverage of the
auto-wire DECISION logic — whether claude-setup writes the PreToolUse group at all.)

Exercised through the REAL CLI (subprocess), stdin piped exactly as Claude Code would.
"""
import json
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
PY = sys.executable
NS = "test:enforce_hook"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def run_hook(ns, stdin_obj, cache_dir):
    env = {**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_NS": ns, "HOME": cache_dir}
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", "enforce"],
                       input=json.dumps(stdin_obj), capture_output=True, text=True,
                       timeout=30, env=env)
    return r.returncode, r.stdout.strip(), r.stderr


def refresh_cache(ns, home):
    """Drive _refresh_enforce_cache the same way `hook status` (SessionStart) would, but
    directly, so cache-population isn't entangled with the rest of the status hook's
    behavior (server health, offline queue, nudges) in this test."""
    env = {**os.environ, "MEMNOS_DSN": DSN, "HOME": home}
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import memnos_cli\n"
        "cfg = memnos_cli.load_config()\n"
        "memnos_cli._refresh_enforce_cache(cfg, %r)\n"
    ) % (ROOT, ns)
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def main():
    import tempfile
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    def reset():
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace=%s", (NS,))
            c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s AND action='constraint.enforce'", (NS,))

    def audit_rows():
        with conn.cursor() as c:
            c.execute("SELECT detail FROM memnos_control.audit_log WHERE namespace=%s "
                      "AND action='constraint.enforce' ORDER BY id", (NS,))
            return [r["detail"] for r in c.fetchall()]

    reset()
    home = tempfile.mkdtemp()

    # --- 1. no cache at all (never refreshed): fails OPEN, no output, no crash ---------------
    print("=== no cache (fail-open) ===")
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, home)
    check("no cache: exits 0", rc == 0)
    check("no cache: prints nothing (defer, not a decision)", out == "")

    # --- 2. cache refreshed, but zero active rules: still fails OPEN ------------------------
    print("=== cache refreshed, zero rules ===")
    refresh_cache(NS, home)
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, home)
    check("zero rules: exits 0, no output", rc == 0 and out == "")

    # --- 3. one block rule, matching call -> deny with exact JSON shape ---------------------
    print("=== block rule, matching call ===")
    Control.add_constraint_enforcement(conn, NS, "never rm -rf without confirmation",
                                       "block", "Bash(rm*)")
    refresh_cache(NS, home)
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}, home)
    check("match: exits 0", rc == 0)
    body = json.loads(out) if out else {}
    hso = body.get("hookSpecificOutput", {})
    check("match: hookEventName == PreToolUse", hso.get("hookEventName") == "PreToolUse")
    check("match: permissionDecision == deny", hso.get("permissionDecision") == "deny")
    check("match: reason mentions the rule text",
          "never rm -rf without confirmation" in (hso.get("permissionDecisionReason") or ""))

    # --- 4. same cache, NON-matching call -> defers (no output) -----------------------------
    print("=== block rule, non-matching call ===")
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, home)
    check("non-match: exits 0, no output", rc == 0 and out == "")
    rc, out, err = run_hook(NS, {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}, home)
    check("different tool entirely: exits 0, no output", rc == 0 and out == "")

    # --- 5. matcher matches on the BARE tool name too (not just the rich subject) -----------
    print("=== bare-tool-name matcher ===")
    reset()
    Control.add_constraint_enforcement(conn, NS, "never touch the Read tool", "ask", "Read")
    refresh_cache(NS, home)
    rc, out, err = run_hook(NS, {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, home)
    body = json.loads(out) if out else {}
    check("bare-name match: permissionDecision == ask",
          body.get("hookSpecificOutput", {}).get("permissionDecision") == "ask")

    # --- 6. block beats ask when both match the same call ------------------------------------
    print("=== block-beats-ask precedence ===")
    reset()
    Control.add_constraint_enforcement(conn, NS, "ask before any bash", "ask", "Bash*")
    Control.add_constraint_enforcement(conn, NS, "block rm specifically", "block", "Bash(rm*)")
    refresh_cache(NS, home)
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}, home)
    body = json.loads(out) if out else {}
    check("both match: block wins over ask",
          body.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")

    # --- 7. every matched decision is audit-logged -------------------------------------------
    print("=== audit logging ===")
    rows = audit_rows()
    check("at least one audit row for the matched calls above", len(rows) >= 1)
    check("audit detail carries level + tool_name + matched rule id",
          all("level" in d and "tool_name" in d and "matched_id" in d for d in rows))

    # --- 8. a broken matcher on one rule fails open for ITSELF, doesn't block a working rule -
    print("=== broken matcher isolation ===")
    reset()
    # an unclosed character class is invalid glob/regex-adjacent syntax fnmatch can choke on
    Control.add_constraint_enforcement(conn, NS, "broken rule", "block", "Bash([")
    Control.add_constraint_enforcement(conn, NS, "working rule", "block", "Bash(rm*)")
    refresh_cache(NS, home)
    rc, out, err = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}, home)
    check("broken matcher doesn't crash the hook", rc == 0)
    body = json.loads(out) if out else {}
    check("the OTHER, working rule still fires",
          body.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
    rc2, out2, err2 = run_hook(NS, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, home)
    check("broken matcher alone (no other match) does NOT block an unrelated call",
          rc2 == 0 and out2 == "")

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
