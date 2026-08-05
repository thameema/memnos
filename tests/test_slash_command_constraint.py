"""Tests for issue #27: in-chat /memnos slash command constraint declaration + cheat sheet.

Two independent surfaces, both covered:
  1. The `/memnos` slash-command template (`memnos_cli._SLASH_CMD`) — its embedded bash
     dispatch must route `constraint <rule>` / `rule <rule>` / `!<rule>` to a pinned
     constraint write, `remember <fact>` to a plain write, `?`/`help`/`cheat` to the
     cheat sheet, and leave the existing ns=/recall behavior untouched. Pure bash-logic
     tests, no server/DB needed.
  2. The MCP `remember(text, memory_type=...)` tool (memnos_mcp.py) — Desktop/MCP users'
     equivalent path. Live-server integration test: a memory_type="constraint" write
     actually gets PINNED into a subsequent /recall on an unrelated query.

Run:
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos \
    MEMNOS_URL=http://127.0.0.1:8900 \
    python tests/test_slash_command_constraint.py
"""
import importlib
import os
import re
import subprocess
import sys
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
import memnos_cli

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:slash_constraint"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


# ---------------------------------------------------------------------------
# 1. bash dispatch logic (no server/DB) — extract the `!`...`` line and run it
#    under a mocked `memnos` shell function that just echoes its argv.
# ---------------------------------------------------------------------------

def _dispatch(argument_string):
    """Run _SLASH_CMD's embedded bash dispatch with $A pre-set (bypassing Claude Code's
    own $ARGUMENTS substitution) and a mocked `memnos` that reports its exact argv,
    one bracketed token per arg — so word-splitting bugs are visible, not masked."""
    m = re.search(r"!`(.*)`", memnos_cli._SLASH_CMD)
    assert m, "_SLASH_CMD must contain a `!`...`` bash dispatch line"
    line = m.group(1)
    line = line.split(";", 1)[1]        # drop the leading A="$ARGUMENTS"; — we set A ourselves
    script = "memnos() { printf '[%s]' \"$@\"; echo; }\nA=\"$1\"\n" + line
    r = subprocess.run(["bash", "-c", script, "bash", argument_string],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def test_dispatch_constraint_verb_pins_as_constraint_type():
    out = _dispatch("constraint always use bash, not zsh")
    assert out == ("[remember][always use bash, not zsh][--type][constraint]"
                    "[--namespace][auto][--token][__MEMNOS_TOKEN__]"), out


def test_dispatch_rule_alias_same_as_constraint():
    out = _dispatch("rule never touch prod without asking")
    assert out == ("[remember][never touch prod without asking][--type][constraint]"
                    "[--namespace][auto][--token][__MEMNOS_TOKEN__]"), out


def test_dispatch_bang_alias_same_as_constraint():
    out = _dispatch("!never delete without confirmation")
    assert out == ("[remember][never delete without confirmation][--type][constraint]"
                    "[--namespace][auto][--token][__MEMNOS_TOKEN__]"), out


def test_dispatch_remember_verb_untyped():
    out = _dispatch("remember the sky is blue today")
    assert out == "[remember][the sky is blue today][--namespace][auto][--token][__MEMNOS_TOKEN__]", out


def test_dispatch_multiword_arg_stays_one_token():
    """Regression guard: a rule/fact with commas/multiple words must not get word-split
    across separate memnos argv positions."""
    out = _dispatch("constraint always run tests, never skip hooks, ask before force-push")
    assert out.count("[") == 8   # remember, <one rule token>, --type, constraint, --namespace, auto, --token, <tok>
    assert "always run tests, never skip hooks, ask before force-push" in out


def test_dispatch_cheat_sheet_produces_no_bash_output():
    """UI-portability fix (issue #27 field report): `!`bash`` stdout is fed to the model's
    context but never RENDERED to the user in compact/mobile UIs (e.g. Orca) — so the cheat
    sheet moved to static Instructions text the model prints itself. The bash branch for
    ?/help/cheat/cheatsheet must now be a true no-op."""
    for arg in ("?", "help", "cheat", "cheatsheet"):
        out = _dispatch(arg)
        assert out == "", f"{arg!r} branch must be a no-op (`:`), got bash output: {out!r}"


def test_dispatch_existing_ns_behavior_unchanged():
    assert _dispatch("") == "[ns]"
    assert _dispatch("ns") == "[ns]"
    assert _dispatch("ns=proj:x") == "[ns][proj:x]"
    assert _dispatch("ns clear") == "[ns][clear]"
    assert _dispatch("ns list") == "[namespace][ls]"


def test_dispatch_ns_prune_runs_safe_dry_run_empty_scan():
    """issue #30: `/memnos ns prune` is READ-ONLY in chat — always --empty --dry-run, never
    --force. Actual deletion stays an explicit CLI step outside the slash command."""
    out = _dispatch("ns prune")
    assert out == "[namespace][prune][--empty][--dry-run]", out
    assert "--force" not in out


def test_dispatch_default_falls_through_to_recall():
    out = _dispatch("what did we decide about auth")
    assert out == "[recall][what did we decide about auth][--namespace][auto][--token][__MEMNOS_TOKEN__]", out


def test_dispatch_plain_word_starting_with_constraint_is_not_misrouted():
    """'constraintx' must NOT match the 'constraint ' (with trailing space) pattern —
    only an actual `constraint <rule>` verb call should route to a typed write."""
    out = _dispatch("constraints on the API design")
    assert out == "[recall][constraints on the API design][--namespace][auto][--token][__MEMNOS_TOKEN__]", out


# ---------------------------------------------------------------------------
# 2. template content — the shipped strings carry the documented guidance
# ---------------------------------------------------------------------------

def test_slash_cmd_instructions_mention_constraint():
    assert "/memnos constraint <rule>" in memnos_cli._SLASH_CMD
    assert "/memnos !<rule>" in memnos_cli._SLASH_CMD


def test_slash_cmd_cheat_sheet_branch_is_noop_bash():
    """Regression guard, updated: the cheat sheet used to be printed via `printf` in the
    ?/help/cheat branch, which needed an extra `allowed-tools` grant and (per field report)
    doesn't even render in compact/mobile UIs since bash stdout isn't shown to the user
    there. It's now static Instructions text the model prints itself, so the bash branch
    must call no external command at all — and `Bash(printf:*)` is no longer needed."""
    m = re.search(r"!`(.*)`", memnos_cli._SLASH_CMD)
    bash_line = m.group(1)
    branch = re.search(r'"\?"\|help\|cheat\|cheatsheet\) (.*?) ;;', bash_line)
    assert branch and branch.group(1).strip() == ":", f"expected a no-op branch, got: {branch}"
    frontmatter = memnos_cli._SLASH_CMD.split("---")[1]
    assert "Bash(printf:*)" not in frontmatter


def test_slash_cmd_cheat_sheet_lives_in_instructions_not_bash():
    bash_line = re.search(r"!`(.*)`", memnos_cli._SLASH_CMD).group(1)
    assert "/memnos constraint <rule>" not in bash_line, \
        "cheat sheet content must not be embedded in bash stdout (invisible on mobile/compact UIs)"
    assert "/memnos constraint <rule>" in memnos_cli._SLASH_CMD
    assert "/memnos remember <fact>" in memnos_cli._SLASH_CMD
    assert "/memnos ns=proj:x" in memnos_cli._SLASH_CMD
    assert "/memnos ns prune" in memnos_cli._SLASH_CMD
    assert "governs behavior" in memnos_cli._SLASH_CMD
    assert "reply with EXACTLY the block below" in memnos_cli._SLASH_CMD


def test_slash_cmd_cheat_sheet_documents_admin_console_and_config():
    assert "__MEMNOS_URL__/admin" in memnos_cli._SLASH_CMD
    assert "memnos token mint admin --label console" in memnos_cli._SLASH_CMD
    assert "'*' grant" in memnos_cli._SLASH_CMD
    assert "~/.memnos/config.json" in memnos_cli._SLASH_CMD
    assert "~/.memnos/server.log" in memnos_cli._SLASH_CMD


def test_slash_cmd_memnos_calls_carry_inline_auth_placeholders():
    """Token-robustness fix (issue #27 field report): a blank config.json admin_token must
    not 401 the slash command. remember/recall (the only two verbs that hit the HTTP Bearer-
    auth API) carry a `--token` CLI ARG, rendered by cmd_claude_setup — never relying on
    config.json. This must be a trailing arg, NOT an `ENV=val` prefix on the command line: a
    Claude Code Bash(memnos:*) allow-rule only auto-strips a small known-safe env-var
    allowlist ahead of the command word, so `MEMNOS_TOKEN=... memnos ...` would silently fail
    to match the grant and turn every /memnos call into a permission prompt (verified against
    Claude Code's own permission-matcher docs, not just bash's argv semantics)."""
    bash_line = re.search(r"!`(.*)`", memnos_cli._SLASH_CMD).group(1)
    assert not re.search(r"MEMNOS_\w+=\S+\s+memnos\b", bash_line), \
        "no memnos invocation may be prefixed with an ENV=val assignment (breaks Bash(memnos:*) matching)"
    calls = re.findall(r"memnos (?:remember|recall)\b[^;]*--token ([^;\s]+)", bash_line)
    assert len(calls) == 3, f"expected remember(constraint)/remember/recall all pass --token, found {len(calls)}"
    assert all(t == "__MEMNOS_TOKEN__" for t in calls)
    # ns / ns clear / namespace ls never call the HTTP API — must NOT carry a token at all.
    for branch_re in (r'ns=\*\) (.*?);;', r'"ns clear"\) (.*?);;', r'"ns list"\|list\|ls\) (.*?);;'):
        branch = re.search(branch_re, bash_line).group(1)
        assert "--token" not in branch and "MEMNOS_" not in branch, branch


def test_desktop_skill_documents_memory_type_constraint():
    assert 'memory_type="constraint"' in memnos_cli._DESKTOP_SKILL
    assert "PINNED" in memnos_cli._DESKTOP_SKILL


# ---------------------------------------------------------------------------
# 3. MCP remember(memory_type=...) — live-server integration: a constraint write is
#    actually PINNED into a subsequent /recall on an unrelated query (issue #27 AC).
# ---------------------------------------------------------------------------

def _call(method, path, token=None, body=None):
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_mcp_remember_constraint_type_is_pinned_into_recall():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    try:
        pid = Control.create_principal(conn, "slashcmd_test_agent", "agent")
    except Exception:
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", ("slashcmd_test_agent",))
            pid = c.fetchone()["id"]
    Control.grant(conn, pid, NS)
    token = Control.mint_token(conn, pid, "slashcmd-test")

    os.environ["MEMNOS_URL"] = URL
    os.environ["MEMNOS_TOKEN"] = token
    os.environ["MEMNOS_NS"] = NS
    import memnos_mcp
    importlib.reload(memnos_mcp)
    remember = getattr(memnos_mcp.remember, "fn", memnos_mcp.remember)

    out = remember("Never deploy to prod on a Friday without explicit sign-off, the slash command constraint marker phrase zephyr quokka.",
                   memory_type="constraint")
    assert "remembered" in out, out

    status, body = _call("POST", "/recall", token=token,
                         body={"namespace": NS, "query": "completely unrelated weather forecast question"})
    assert status == 200, body
    mems = body.get("memories", [])
    pins = [m for m in mems if m.get("pinned")]
    assert pins, f"expected at least one pinned memory, got: {mems}"
    assert any("the slash command constraint marker phrase zephyr quokka" in (m.get("content") or "") for m in pins), pins
    assert all(m.get("type") == "constraint" for m in pins)
    assert body.get("context", "").startswith("CONSTRAINT:")

    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (NS,))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            check(t.__name__, True)
        except Exception as e:
            check(f"{t.__name__} -- {e}", False)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
