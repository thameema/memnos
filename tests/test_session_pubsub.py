"""Named session registry + channel pub/sub bus (issue #231).

Runs against a live local server + its Postgres (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=http://127.0.0.1:8900 pytest tests/test_session_pubsub.py
No LLM / OpenAI key needed.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sdk"))
from core.control import Control  # noqa: E402

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:session-pubsub"
SCHEMA = "tenant_memnos"


def call(path, token, body):
    req = urllib.request.Request(URL + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture(scope="module")
def env():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    def wipe():
        with conn.cursor() as c:
            for t in ("channel_subscriptions", "channel_messages", "agent_sessions"):
                c.execute(f"DELETE FROM memnos_control.{t} WHERE namespace=%s", (NS,))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    wipe()
    ids, toks = {}, {}
    for tag in ("pa", "pb"):
        name = f"sps-{tag}"
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (name,))
            row = c.fetchone()
        ids[tag] = row["id"] if row else Control.create_principal(conn, name, "service")
        Control.grant(conn, ids[tag], NS)
        toks[tag] = Control.mint_token(conn, ids[tag], "sps")
    Control.create_namespace(conn, NS, created_by=ids["pa"])
    yield {"conn": conn, "ta": toks["pa"], "tb": toks["pb"], "pa": ids["pa"], "pb": ids["pb"],
           "na": "sps-pa", "nb": "sps-pb"}
    wipe()
    with conn.cursor() as c:
        for tag in ("pa", "pb"):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (ids[tag],))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (ids[tag],))
            c.execute("DELETE FROM memnos_control.namespaces WHERE created_by=%s", (ids[tag],))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (ids[tag],))
    conn.close()


def test_00_register_two_sessions_and_list(env):
    s, r = call("/session/register", env["ta"], {"namespace": NS, "name": "@gemini-critic",
                                                 "role": "critic", "harness": "gemini", "channels": ["review"]})
    assert s == 200 and r["name"] == "gemini-critic"
    assert {"@gemini-critic", "#general", "#review"} <= set(r["subscribed_channels"])
    s, r = call("/session/register", env["tb"], {"namespace": NS, "name": "claude-dev",
                                                 "role": "implementer", "harness": "claude-code",
                                                 "channels": ["#review"]})
    assert s == 200
    s, r = call("/session/list", env["ta"], {"namespace": NS})
    by = {x["name"]: x for x in r["sessions"]}
    assert set(by) == {"gemini-critic", "claude-dev"}
    assert by["gemini-critic"]["author"] == env["na"] and by["claude-dev"]["author"] == env["nb"]
    assert by["claude-dev"]["active"] and by["claude-dev"]["unread"] == 0
    s, r = call("/session/list", env["ta"], {"namespace": NS, "role": "critic"})
    assert [x["name"] for x in r["sessions"]] == ["gemini-critic"]


def test_01_routing_cursor_peek_self_filter_and_authorship(env):
    ta, tb = env["ta"], env["tb"]
    # critic -> topic channel + direct inbox. author is server-stamped from the token.
    s, m1 = call("/channel/publish", ta, {"namespace": NS, "channel": "#review", "sender_session": "gemini-critic",
                                          "content": "please review X", "intent": "review_request"})
    assert s == 200 and m1["author"] == env["na"]
    s, m2 = call("/channel/publish", ta, {"namespace": NS, "channel": "@claude-dev", "sender_session": "gemini-critic",
                                          "content": "direct note", "intent": "critique"})
    assert s == 200
    # client-supplied author is ignored (cannot spoof)
    s, m3 = call("/channel/publish", ta, {"namespace": NS, "channel": "#review", "sender_session": "gemini-critic",
                                          "content": "spoof?", "author": env["nb"], "principal_id": env["pb"]})
    assert m3["author"] == env["na"]

    s, r = call("/session/list", tb, {"namespace": NS})
    assert {x["name"]: x for x in r["sessions"]}["claude-dev"]["unread"] == 3

    s, r = call("/channel/poll", tb, {"namespace": NS, "session_name": "claude-dev", "peek": True})
    assert [m["id"] for m in r["messages"]] == [m1["id"], m2["id"], m3["id"]]
    s, r = call("/channel/poll", tb, {"namespace": NS, "session_name": "claude-dev", "peek": True})
    assert r["count"] == 3, "peek must not advance the cursor"
    s, r = call("/channel/poll", tb, {"namespace": NS, "session_name": "claude-dev"})
    assert r["count"] == 3 and {m["channel"] for m in r["messages"]} == {"#review", "@claude-dev"}
    assert all(m["author"] == env["na"] for m in r["messages"])
    s, r = call("/channel/poll", tb, {"namespace": NS, "session_name": "claude-dev"})
    assert r["count"] == 0, "cursor advanced"

    # self-filter: own #review post is not delivered back to the sender
    call("/channel/publish", tb, {"namespace": NS, "channel": "#review", "sender_session": "claude-dev",
                                  "content": "done with X", "intent": "done"})
    s, r = call("/channel/poll", tb, {"namespace": NS, "session_name": "claude-dev"})
    assert r["count"] == 0
    s, r = call("/channel/poll", ta, {"namespace": NS, "session_name": "gemini-critic"})
    assert [m["content"] for m in r["messages"]] == ["done with X"]


def test_02_poll_is_scoped_to_owning_principal(env):
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@claude-dev", "sender_session": "gemini-critic",
                                         "content": "private"})
    # principal B polling a session name owned by A sees nothing and moves nothing
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "gemini-critic"})
    assert s == 200 and r["count"] == 0
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev"})
    assert [m["content"] for m in r["messages"]] == ["private"]


def test_03_session_name_conflict_and_takeover_after_expiry(env):
    s, r = call("/session/register", env["tb"], {"namespace": NS, "name": "gemini-critic"})
    assert s == 409, "different principal must not hijack an active session"
    s, r = call("/session/register", env["ta"], {"namespace": NS, "name": "gemini-critic", "role": "critic"})
    assert s == 200, "same principal may re-register"
    with env["conn"].cursor() as c:
        c.execute("UPDATE memnos_control.agent_sessions SET expires_at=now()-interval '1 second' "
                  "WHERE namespace=%s AND name='gemini-critic'", (NS,))
    s, r = call("/session/list", env["ta"], {"namespace": NS})
    assert "gemini-critic" not in [x["name"] for x in r["sessions"]]
    s, r = call("/session/list", env["ta"], {"namespace": NS, "include_expired": True})
    assert any(x["name"] == "gemini-critic" and not x["active"] for x in r["sessions"])
    s, r = call("/session/register", env["tb"], {"namespace": NS, "name": "gemini-critic"})
    assert s == 200, "expired name can be taken over"
    # old owner's subscriptions did not carry over
    s, r = call("/channel/poll", env["ta"], {"namespace": NS, "session_name": "gemini-critic"})
    assert r["count"] == 0
    call("/session/unregister", env["tb"], {"namespace": NS, "name": "gemini-critic"})
    s, r = call("/session/register", env["ta"], {"namespace": NS, "name": "gemini-critic"})
    assert s == 200, "offline name is free again"


def test_04_heartbeat_publishes_status_and_wildcards(env):
    call("/channel/subscribe", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channels": ["status:*"]})
    s, r = call("/session/heartbeat", env["ta"], {"namespace": NS, "name": "gemini-critic",
                                                  "current_task": "reviewing PR 7"})
    assert s == 200 and r["current_task"] == "reviewing PR 7"
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev"})
    st = [m for m in r["messages"] if m["channel"] == "status:gemini-critic"]
    assert st and st[0]["intent"] == "status" and st[0]["content"] == "reviewing PR 7"
    # same task again -> no duplicate status
    call("/session/heartbeat", env["ta"], {"namespace": NS, "name": "gemini-critic", "current_task": "reviewing PR 7"})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev"})
    assert r["count"] == 0
    s, r = call("/session/heartbeat", env["tb"], {"namespace": NS, "name": "gemini-critic"})
    assert s == 404, "cannot heartbeat someone else's session"
    # '*' wildcard: everything except other sessions' direct inboxes
    call("/channel/subscribe", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channels": ["*"]})
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#design", "sender_session": "gemini-critic", "content": "arch"})
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@someone-else", "sender_session": "gemini-critic", "content": "x"})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev"})
    assert [m["content"] for m in r["messages"]] == ["arch"], "no dupes, no foreign inboxes"
    s, r = call("/channel/unsubscribe", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channels": ["*", "status:*"]})
    assert r["unsubscribed"] == 2


def test_05_explicit_unsubscribed_channel_poll_and_from_beginning(env):
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#late", "sender_session": "gemini-critic", "content": "old"})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channel": "#late"})
    assert [m["content"] for m in r["messages"]] == ["old"]
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channel": "#late"})
    assert r["count"] == 0
    call("/channel/subscribe", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channels": ["#late"], "from_beginning": True})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channel": "#late"})
    assert r["count"] == 1


def test_06_wait_returns_on_concurrent_publish_and_times_out(env):
    call("/channel/subscribe", env["tb"], {"namespace": NS, "session_name": "claude-dev", "channels": ["#review"]})
    threading.Timer(1.0, lambda: call("/channel/publish", env["ta"], {
        "namespace": NS, "channel": "#review", "sender_session": "gemini-critic", "content": "wake up"})).start()
    t0 = time.monotonic()
    s, r = call("/channel/wait", env["tb"], {"namespace": NS, "session_name": "claude-dev", "timeout_seconds": 20})
    dt = time.monotonic() - t0
    assert s == 200 and r["count"] == 1 and r["timed_out"] is False
    assert r["messages"][0]["content"] == "wake up" and dt < 10, f"woke in {dt:.1f}s"
    t0 = time.monotonic()
    s, r = call("/channel/wait", env["tb"], {"namespace": NS, "session_name": "claude-dev", "timeout_seconds": 0})
    assert r["timed_out"] is True and r["count"] == 0 and 0.5 < time.monotonic() - t0 < 5, "timeout clamped to >=1s"


def test_07_remember_persists_raw_turn(env):
    s, r = call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#review", "sender_session": "gemini-critic",
                                                "content": "DECISION: ship the registry", "intent": "done", "remember": True})
    assert s == 200 and r["remembered"] is True
    with env["conn"].cursor() as c:
        c.execute(f"SELECT speaker, text, memory_type FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        rows = c.fetchall()
    assert any("ship the registry" in x["text"] and x["memory_type"] == "decision" for x in rows)
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#review", "sender_session": "gemini-critic", "content": "not saved"})
    with env["conn"].cursor() as c:
        c.execute(f"SELECT count(*) n FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text LIKE %s", (NS, "%not saved%"))
        assert c.fetchone()["n"] == 0


def test_08_validation_and_acl(env):
    for body in ({"channel": "#x", "content": "", "sender_session": "a"},
                 {"channel": "#x", "content": "hi", "sender_session": "a", "intent": "bogus"},
                 {"channel": "*", "content": "hi", "sender_session": "a"},
                 {"channel": "#bad channel", "content": "hi", "sender_session": "a"},
                 {"channel": "#x", "content": "hi", "sender_session": ""}):
        s, r = call("/channel/publish", env["ta"], {"namespace": NS, **body})
        assert s == 400, body
    s, _ = call("/session/register", env["ta"], {"namespace": NS, "name": "bad name!"})
    assert s == 400
    s, _ = call("/channel/publish", env["ta"], {"namespace": "test:no-grant-sps", "channel": "#x", "content": "hi", "sender_session": "a"})
    assert s == 403
    s, _ = call("/channel/list", "mnk_bogus", {"namespace": NS})
    assert s == 401


def test_09_channel_list(env):
    s, r = call("/channel/list", env["ta"], {"namespace": NS})
    by = {c["channel"]: c for c in r["channels"]}
    assert by["#review"]["message_count"] >= 3 and "claude-dev" in by["#review"]["subscribers"]
    assert "@claude-dev" in by and "#general" in by


def test_10_sdk_roundtrip(env):
    from memnos_sdk import MemnosClient
    with MemnosClient(base_url=URL, token=env["tb"], namespace=NS) as mem:
        mem.channel_publish("#sdk", "hello from sdk", sender_session="claude-dev")
        assert any(x["name"] == "claude-dev" for x in mem.session_list()["sessions"])
        assert mem.channel_wait("claude-dev", timeout_seconds=1)["timed_out"] in (True, False)
        assert any(c["channel"] == "#sdk" for c in mem.channel_list()["channels"])


def _cli(args, env, stdin=None, session=None):
    e = {**os.environ, "MEMNOS_URL": URL, "MEMNOS_TOKEN": env["tb"], "MEMNOS_NS": NS,
         "MEMNOS_UPDATE_CHECK": "0", "HOME": os.environ.get("SPS_HOME", "/tmp/sps-home")}
    os.makedirs(e["HOME"], exist_ok=True)
    e.pop("MEMNOS_SESSION", None)
    if session:
        e["MEMNOS_SESSION"] = session
    return subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py")] + args, env=e, input=stdin,
                          capture_output=True, text=True, timeout=60, cwd="/tmp")


def test_11_cli_session_and_channel(env):
    r = _cli(["session", "ls", "--namespace", NS], env)
    assert r.returncode == 0 and "claude-dev" in r.stdout, r.stdout + r.stderr
    r = _cli(["channel", "pub", "#cli", "from cli", "--session", "claude-dev", "--namespace", NS], env)
    assert r.returncode == 0 and "published" in r.stdout, r.stdout + r.stderr
    r = _cli(["channel", "ls", "--namespace", NS], env)
    assert "#cli" in r.stdout


def test_12_hook_recall_injects_inbox(env):
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@claude-dev", "sender_session": "gemini-critic",
                                         "content": "INBOX-MARKER please fix", "intent": "critique"})
    payload = json.dumps({"prompt": "what is next", "session_id": "sps-hook"})
    r = _cli(["hook", "recall"], env, stdin=payload, session="claude-dev")
    assert r.returncode == 0, r.stderr
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert '<memnos:inbox session="claude-dev">' in ctx and "INBOX-MARKER please fix" in ctx
    assert "[@claude-dev] gemini-critic (critique" in ctx
    # consumed: next turn has no inbox; and without MEMNOS_SESSION nothing is polled at all
    r = _cli(["hook", "recall"], env, stdin=payload, session="claude-dev")
    assert "memnos:inbox" not in r.stdout
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@claude-dev", "sender_session": "gemini-critic", "content": "second"})
    r = _cli(["hook", "recall"], env, stdin=payload)
    assert "memnos:inbox" not in r.stdout
    s, p = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "claude-dev"})
    assert p["count"] == 1, "message untouched when MEMNOS_SESSION unset"


def test_13_hook_recall_combines_inbox_and_recalled_memories(monkeypatch, capsys, tmp_path):
    import argparse
    import io
    import memnos_cli as cli
    import nsresolve

    def fake_urlopen(req, timeout=8):
        if req.full_url.endswith("/channel/poll"):
            body = {"messages": [{"id": 42, "channel": "#review", "sender_session": "gemini-critic",
                                  "intent": "critique", "author": "alice", "content": "Fix retry loop"}]}
        elif req.full_url.endswith("/recall"):
            body = {"context": "- Retry budget is 3 attempts", "memories": [{"id": 1}]}
        else:
            body = {}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMNOS_SESSION", "claude-dev")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(nsresolve, "resolve_with_source", lambda d=None: ("proj:test", "env"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "how should retry work", "session_id": "s13"})))
    capsys.readouterr()
    cli.cmd_hook(argparse.Namespace(which="recall"), {"port": 8900})
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.index('<memnos:inbox session="claude-dev">') < ctx.index("## Relevant memories (memnos)")
    assert "Fix retry loop" in ctx and "Retry budget is 3 attempts" in ctx



def test_14_chat_registered_session_binds_inbox_without_env(env, monkeypatch):
    """Register in chat (no MEMNOS_SESSION anywhere) -> hook recall injects the inbox for this cwd."""
    import nsresolve
    home = os.environ.get("SPS_HOME", "/tmp/sps-home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("MEMNOS_SESSION", raising=False)
    monkeypatch.delenv("MEMNOS_CONFIG_DIR", raising=False)
    nsresolve.clear_active_session(cwd="/tmp")      # stale binding from an earlier run
    s, _ = call("/session/register", env["tb"], {"namespace": NS, "name": "chat-bound"})
    assert s == 200
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@chat-bound",
                                         "sender_session": "gemini-critic", "content": "BOUND-MARKER"})
    payload = json.dumps({"prompt": "what is next", "session_id": "sps-bound"})
    assert nsresolve.resolve_active_session(cwd="/tmp") == ""
    r = _cli(["hook", "recall"], env, stdin=payload)
    assert "memnos:inbox" not in r.stdout          # unbound: nothing injected
    nsresolve.save_active_session("@chat-bound", cwd="/tmp", namespace=NS)
    assert nsresolve.resolve_active_session(cwd="/tmp") == "chat-bound"
    r = _cli(["hook", "recall"], env, stdin=payload)
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert '<memnos:inbox session="chat-bound">' in ctx and "BOUND-MARKER" in ctx, r.stdout + r.stderr
    # unregister clears the binding
    nsresolve.clear_active_session(name="chat-bound", cwd="/tmp")
    assert nsresolve.resolve_active_session(cwd="/tmp") == ""


def test_15_server_resolves_session_by_host_and_cwd(env):
    s, _ = call("/session/register", env["tb"], {"namespace": NS, "name": "cwd-bound",
                                                 "host": "h-test", "cwd": "/work/x"})
    assert s == 200
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@cwd-bound",
                                         "sender_session": "gemini-critic", "content": "CWD-MARKER"})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "host": "h-test", "cwd": "/work/x"})
    assert s == 200 and r["session_name"] == "cwd-bound" and r["count"] == 1
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "host": "h-test", "cwd": "/work/none"})
    assert s == 200 and r == {"session_name": "", "messages": [], "count": 0}
    s, r = call("/channel/poll", env["tb"], {"namespace": NS})
    assert s == 400
    call("/session/unregister", env["tb"], {"namespace": NS, "name": "cwd-bound"})
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "host": "h-test", "cwd": "/work/x"})
    assert r["session_name"] == ""      # offline sessions are not resolved


def test_16_transcript_agent_name_auto_resolves_for_inbox(env, tmp_path):
    """A Claude/Tommy session with agentName/customTitle in its transcript (e.g. 'Tommy | MEMNOS')
    automatically resolves as 'tommy-memnos' and receives @tommy-memnos messages via hook recall."""
    import nsresolve
    tp = tmp_path / "730d3723.jsonl"
    tp.write_text(
        json.dumps({"type": "custom-title", "customTitle": "Tommy | MEMNOS | 730d3723"}) + "\n"
        + json.dumps({"type": "agent-name", "agentName": "Tommy | MEMNOS"}) + "\n",
        encoding="utf-8",
    )
    assert nsresolve.resolve_active_session(cwd=str(tmp_path), data={"transcript_path": str(tp)}) == "tommy-memnos"
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@tommy-memnos",
                                         "sender_session": "gemini-critic", "content": "HELLO-TOMMY-MEMNOS"})
    payload = json.dumps({"prompt": "check peer inbox", "session_id": "730d3723",
                          "cwd": str(tmp_path), "transcript_path": str(tp)})
    r = _cli(["hook", "recall"], env, stdin=payload)
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert '<memnos:inbox session="tommy-memnos">' in ctx and "HELLO-TOMMY-MEMNOS" in ctx, r.stdout + r.stderr
