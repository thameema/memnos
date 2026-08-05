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
    assert out == "[remember][always use bash, not zsh][--type][constraint][--namespace][auto]", out


def test_dispatch_rule_alias_same_as_constraint():
    out = _dispatch("rule never touch prod without asking")
    assert out == "[remember][never touch prod without asking][--type][constraint][--namespace][auto]", out


def test_dispatch_bang_alias_same_as_constraint():
    out = _dispatch("!never delete without confirmation")
    assert out == "[remember][never delete without confirmation][--type][constraint][--namespace][auto]", out


def test_dispatch_remember_verb_untyped():
    out = _dispatch("remember the sky is blue today")
    assert out == "[remember][the sky is blue today][--namespace][auto]", out


def test_dispatch_multiword_arg_stays_one_token():
    """Regression guard: a rule/fact with commas/multiple words must not get word-split
    across separate memnos argv positions."""
    out = _dispatch("constraint always run tests, never skip hooks, ask before force-push")
    assert out.count("[") == 6          # remember, <one rule token>, --type, constraint, --namespace, auto
    assert "always run tests, never skip hooks, ask before force-push" in out


def test_dispatch_cheat_sheet_on_question_mark():
    out = _dispatch("?")
    assert "/memnos constraint <rule>" in out
    assert "/memnos remember <fact>" in out
    assert "/memnos ns=proj:x" in out
    assert "governs behavior" in out


def test_dispatch_cheat_sheet_aliases():
    for alias in ("help", "cheat"):
        out = _dispatch(alias)
        assert "/memnos constraint <rule>" in out, f"alias {alias!r} did not show the cheat sheet"


def test_dispatch_existing_ns_behavior_unchanged():
    assert _dispatch("") == "[ns]"
    assert _dispatch("ns") == "[ns]"
    assert _dispatch("ns=proj:x") == "[ns][proj:x]"
    assert _dispatch("ns clear") == "[ns][clear]"
    assert _dispatch("ns list") == "[namespace][ls]"


def test_dispatch_default_falls_through_to_recall():
    out = _dispatch("what did we decide about auth")
    assert out == "[recall][what did we decide about auth][--namespace][auto]", out


def test_dispatch_plain_word_starting_with_constraint_is_not_misrouted():
    """'constraintx' must NOT match the 'constraint ' (with trailing space) pattern —
    only an actual `constraint <rule>` verb call should route to a typed write."""
    out = _dispatch("constraints on the API design")
    assert out == "[recall][constraints on the API design][--namespace][auto]", out


# ---------------------------------------------------------------------------
# 2. template content — the shipped strings carry the documented guidance
# ---------------------------------------------------------------------------

def test_slash_cmd_instructions_mention_constraint():
    assert "/memnos constraint <rule>" in memnos_cli._SLASH_CMD
    assert "/memnos !<rule>" in memnos_cli._SLASH_CMD


def test_slash_cmd_allows_printf_tool():
    """Regression guard: the cheat-sheet branch calls printf, so allowed-tools must
    grant it explicitly or /memnos ? hits a permission prompt instead of just working
    (caught by hands-on testing in Claude Code itself, not by the bash-logic tests
    above — those exercise the dispatch script directly, bypassing the tool-permission
    gate a real Claude Code session enforces)."""
    frontmatter = memnos_cli._SLASH_CMD.split("---")[1]
    assert "Bash(printf:*)" in frontmatter


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
