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
NS_OTHER = "test:session-pubsub-other"
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
            for ns in (NS, NS_OTHER):
                # agent_inbox first: it FKs principals (and itself)
                for t in ("agent_inbox", "channel_subscriptions", "channel_messages", "agent_sessions",
                          "channel_catalog"):
                    c.execute(f"DELETE FROM memnos_control.{t} WHERE namespace=%s", (ns,))
                c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
    wipe()
    ids, toks = {}, {}
    for tag in ("pa", "pb", "pc"):
        name = f"sps-{tag}"
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (name,))
            row = c.fetchone()
        ids[tag] = row["id"] if row else Control.create_principal(conn, name, "service")
        Control.grant(conn, ids[tag], NS_OTHER if tag == "pc" else NS)   # pc: NS_OTHER only
        toks[tag] = Control.mint_token(conn, ids[tag], "sps")
    Control.create_namespace(conn, NS, created_by=ids["pa"])
    Control.create_namespace(conn, NS_OTHER, created_by=ids["pc"])
    yield {"conn": conn, "ta": toks["pa"], "tb": toks["pb"], "tc": toks["pc"], "pa": ids["pa"], "pb": ids["pb"],
           "na": "sps-pa", "nb": "sps-pb"}
    wipe()
    with conn.cursor() as c:
        for tag in ("pa", "pb", "pc"):
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


def test_17_register_auto_advertises_and_returns_peers(env):
    call("/session/register", env["ta"], {"namespace": NS, "name": "adv-a", "role": "critic", "harness": "gemini"})
    s, r = call("/session/register", env["tb"], {
        "namespace": NS, "name": "adv-b", "role": "dev", "harness": "claude-code", "channels": ["#adv-topic"],
        "channel_ads": {"#adv-topic": {"description": "ADV-DESC", "what_you_get": "ADV-WYG", "intents": ["task", "done"]}}})
    assert s == 200 and r["direct_channel"] == "@adv-b" and r["status_channel"] == "status:adv-b"
    assert "adv-a" in [p["name"] for p in r["peers"]] and "adv-b" not in [p["name"] for p in r["peers"]]
    by = {c["channel"]: c for c in r["available_channels"]}
    assert {"@adv-b", "status:adv-b", "#general", "#adv-topic"} <= set(by)
    assert "role=dev, harness=claude-code" in by["@adv-b"]["description"] and by["@adv-b"]["owner_session"] == "adv-b"
    assert by["#adv-topic"]["description"] == "ADV-DESC" and by["#adv-topic"]["intents"] == ["task", "done"]
    assert by["#general"]["description"].startswith("Default namespace-wide")


def test_18_advertise_and_list_rest_mcp_sdk_cli(env):
    s, r = call("/channel/advertise", env["ta"], {"namespace": NS, "channel": "#rest-ad", "description": "REST-D",
                                                  "what_you_get": "REST-W", "intents": ["question", "answer"],
                                                  "owner_session": "adv-a"})
    assert s == 200 and r["description"] == "REST-D" and r["author"] == env["na"]
    s, _ = call("/channel/advertise", env["ta"], {"namespace": NS, "channel": "#rest-ad", "intents": ["bogus"]})
    assert s == 400
    call("/channel/subscribe", env["tb"], {"namespace": NS, "session_name": "adv-b", "channels": ["#rest-ad"]})
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#rest-ad", "sender_session": "adv-a", "content": "x"})
    # blank re-advertise keeps text; publish/subscribe with description updates it
    call("/channel/advertise", env["ta"], {"namespace": NS, "channel": "#rest-ad"})
    s, r = call("/channel/list", env["ta"], {"namespace": NS})
    by = {c["channel"]: c for c in r["channels"]}
    ad = by["#rest-ad"]
    assert (ad["description"], ad["what_you_get"], ad["intents"]) == ("REST-D", "REST-W", ["question", "answer"])
    assert ad["subscribers"] == ["adv-b"] and ad["publishers"] == ["adv-a"] and ad["kind"] == "topic"
    assert {"adv-a", "adv-b"} <= {x["name"] for x in r["active_sessions"]}
    assert [w["channel"] for w in r["wildcard_subscriptions"]] == ["status:*", "*"]
    assert "*" not in by and "status:*" not in by
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "#pub-ad", "sender_session": "adv-a",
                                         "content": "y", "description": "PUB-D"})
    s, r = call("/channel/list", env["ta"], {"namespace": NS})
    assert {c["channel"]: c for c in r["channels"]}["#pub-ad"]["description"] == "PUB-D"
    # SDK
    from memnos_sdk import MemnosClient
    with MemnosClient(base_url=URL, token=env["tb"], namespace=NS) as mem:
        out = mem.channel_advertise("#sdk-ad", "SDK-D", what_you_get="SDK-W", intents=["message"], owner_session="adv-b")
        assert out["description"] == "SDK-D"
        assert mem.session_register("adv-b", channel_ads={"#sdk-ad2": "SDK2-D"}, channels=["#sdk-ad2"])["available_channels"]
    # MCP tool (module-level function, calls the same REST endpoint)
    import memnos_mcp
    env_old = {k: os.environ.get(k) for k in ("MEMNOS_URL", "MEMNOS_TOKEN", "MEMNOS_NS")}
    os.environ.update({"MEMNOS_URL": URL, "MEMNOS_TOKEN": env["tb"], "MEMNOS_NS": NS})
    try:
        res = memnos_mcp.channel_advertise("#mcp-ad", "MCP-D", what_you_get="MCP-W", intents=["task"], owner_session="adv-b")
        assert "MCP-D" in res, res
    finally:
        for k, v in env_old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    # CLI
    r = _cli(["channel", "advertise", "#cli-ad", "-d", "CLI-D", "-w", "CLI-W", "--intents", "task,done",
              "--session", "adv-b", "--namespace", NS], env)
    assert r.returncode == 0 and "CLI-D" in r.stdout, r.stdout + r.stderr
    r = _cli(["channel", "ls", "--namespace", NS], env)
    assert "CLI-D" in r.stdout and "gets: CLI-W" in r.stdout and "topic" in r.stdout


def test_19_recall_with_peers_and_inbox_single_call(env):
    call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@adv-b", "sender_session": "adv-a",
                                         "content": "RECALL-INBOX-MARKER"})
    s, r = call("/recall", env["tb"], {"namespace": NS, "query": "anything", "include_peers": True})
    assert s == 200 and "inbox" not in r
    assert "adv-a" in [x["name"] for x in r["peer_directory"]["active_sessions"]]
    s, r = call("/recall", env["tb"], {"namespace": NS, "query": "anything", "session_name": "adv-b", "poll_inbox": True})
    assert s == 200 and "channels" in r["peer_directory"]
    assert any(m["content"] == "RECALL-INBOX-MARKER" for m in r["inbox"])
    s, r = call("/recall", env["tb"], {"namespace": NS, "query": "anything"})
    assert "peer_directory" not in r and "inbox" not in r


def test_20_hook_recall_injects_peers_block_once(env):
    import uuid
    payload = json.dumps({"prompt": "who is around", "session_id": "sps-peers-" + uuid.uuid4().hex[:8]})
    r = _cli(["hook", "recall"], env, stdin=payload, session="adv-b")
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f'<memnos:peers namespace="{NS}">' in ctx, r.stdout + r.stderr
    assert "peer adv-a" in ctx and "direct=@adv-a" in ctx and "status=status:adv-a" in ctx
    assert "channel #rest-ad: REST-D | gets: REST-W" in ctx and "peer adv-b" not in ctx
    r = _cli(["hook", "recall"], env, stdin=payload, session="adv-b")
    assert "memnos:peers" not in r.stdout, "peers block only on the first turn"


# ---- email-style agent inbox queue (issue #231) ----------------------------------------

def _inbox_sessions(env):
    for tok, name in ((env["ta"], "inb-send"), (env["tb"], "inb-recv")):
        s, _ = call("/session/register", tok, {"namespace": NS, "name": name})
        assert s == 200


def _isend(env, content, tok=None, frm="inb-send", to="inb-recv", **kw):
    s, r = call("/inbox/send", tok or env["ta"], {"namespace": NS, "to_session": to, "sender_session": frm,
                                                   "content": content, **kw})
    assert s == 200, r
    return r


def _ilist(env, tok=None, to="inb-recv", **kw):
    s, r = call("/inbox/list", tok or env["tb"], {"namespace": NS, "to_session": to, **kw})
    assert s == 200, r
    return r


def test_21_namespace_and_acl_isolation(env):
    _inbox_sessions(env)
    call("/channel/advertise", env["ta"], {"namespace": NS, "channel": "#iso-chan", "description": "iso"})
    _isend(env, "ISO-SECRET")
    # a token granted only on NS_OTHER sees nothing from NS, even with the same session name
    s, _ = call("/session/register", env["tc"], {"namespace": NS_OTHER, "name": "inb-recv"})
    assert s == 200
    s, r = call("/session/list", env["tc"], {"namespace": NS_OTHER})
    assert {x["name"] for x in r["sessions"]} == {"inb-recv"}
    s, r = call("/channel/list", env["tc"], {"namespace": NS_OTHER})
    assert "#iso-chan" not in {c["channel"] for c in r["channels"]}
    s, r = call("/inbox/list", env["tc"], {"namespace": NS_OTHER, "to_session": "inb-recv", "status": "all"})
    assert s == 200 and r["count"] == 0 and "ISO-SECRET" not in json.dumps(r)
    for path, body in (("/session/list", {}), ("/channel/list", {}), ("/inbox/list", {"to_session": "inb-recv"})):
        s, _ = call(path, env["tc"], {"namespace": NS, **body})
        assert s == 403, path
    # and the NS tokens (no grant on NS_OTHER) get 403 on both read and write ops there
    for path, body in (("/channel/list", {}),
                       ("/channel/advertise", {"channel": "#x", "description": "d"}),
                       ("/inbox/send", {"to_session": "inb-recv", "sender_session": "inb-send", "content": "x"}),
                       ("/inbox/list", {"to_session": "inb-recv"})):
        s, _ = call(path, env["ta"], {"namespace": NS_OTHER, **body})
        assert s == 403, path
    # within NS, another principal cannot read a session inbox registered to someone else
    assert _ilist(env, tok=env["ta"], to="inb-recv")["count"] == 0
    assert _ilist(env)["count"] >= 1


def test_22_inbox_priority_and_replace_in_queue(env):
    _inbox_sessions(env)
    base = {m["id"] for m in _ilist(env, status="all")["items"]}
    a = _isend(env, "A normal", subject="a", thread_key="t-a")
    b = _isend(env, "B urgent", priority="urgent")
    c = _isend(env, "C high", priority="high")
    a2 = _isend(env, "A2 replaces A", subject="a", replaces_id=a["id"])
    assert a2["superseded_ids"] == [a["id"]] and a["status"] == "pending"
    d = _isend(env, "D low", priority="low", thread_key="t-d")
    e = _isend(env, "E low replaces D by thread", priority="low", thread_key="t-d", replace_pending=True)
    assert e["superseded_ids"] == [d["id"]]
    mine = [m for m in _ilist(env)["items"] if m["id"] not in base]
    assert [m["content"] for m in mine] == ["B urgent", "C high", "A2 replaces A", "E low replaces D by thread"]
    sup = {m["id"]: m for m in _ilist(env, status="superseded")["items"]}
    assert sup[a["id"]]["superseded_by_id"] == a2["id"] and sup[d["id"]]["superseded_by_id"] == e["id"]
    assert [m["id"] for m in _ilist(env, priority="urgent")["items"] if m["id"] not in base] == [b["id"]]
    # another principal can't supersede someone else's item, even with the id
    r = _isend(env, "hijack", tok=env["tb"], frm="inb-recv", to="inb-send", replaces_id=c["id"])
    assert r["superseded_ids"] == []
    assert any(m["id"] == c["id"] for m in _ilist(env)["items"])
    # validation
    for bad in ({"priority": "asap"}, {"intent": "bogus"}, {"content": ""}):
        s, _ = call("/inbox/send", env["ta"], {"namespace": NS, "to_session": "inb-recv",
                                               "sender_session": "inb-send", **{"content": "x", **bad}})
        assert s == 400, bad


def test_23_inbox_recall_and_cancellation(env):
    _inbox_sessions(env)
    payload = json.dumps({"prompt": "go", "session_id": "sps-inbox-hook"})
    # recall before the recipient acts: gone from list + hook, act reports cancelled
    m = _isend(env, "RECALL-ME-MARKER")
    s, r = call("/inbox/recall", env["ta"], {"namespace": NS, "sender_session": "inb-send",
                                             "message_id": m["id"], "reason": "wrong repo"})
    assert s == 200 and r["recalled"] and not r["already_acted"] and r["item"]["status"] == "recalled"
    assert all(x["id"] != m["id"] for x in _ilist(env)["items"])
    assert any(x["id"] == m["id"] for x in _ilist(env, status="recalled")["items"])
    r = _cli(["hook", "recall"], env, stdin=payload, session="inb-recv")
    assert "RECALL-ME-MARKER" not in r.stdout
    s, r = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv", "message_id": m["id"]})
    assert s == 200 and r["ok"] is False and r["cancelled"] and r["status"] == "recalled"
    assert r["recall_reason"] == "wrong repo"
    # a live item shows up once in the hook (marked read), with the how-to-finish hint
    live = _isend(env, "LIVE-HOOK-MARKER", subject="do it", priority="high")
    r = _cli(["hook", "recall"], env, stdin=payload, session="inb-recv")
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"#{live['id']} [high] do it" in ctx and "LIVE-HOOK-MARKER" in ctx and "inbox_act(message_id=" in ctx
    assert "LIVE-HOOK-MARKER" not in _cli(["hook", "recall"], env, stdin=payload, session="inb-recv").stdout
    assert _ilist(env, status="read")["items"][0]["read_at"]
    # cannot recall what is already acted on
    s, r = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv",
                                          "message_id": live["id"], "action": "done", "resolution": "ok"})
    assert s == 200 and r["ok"] and r["item"]["status"] == "acted" and r["item"]["acted_at"]
    s, r = call("/inbox/recall", env["ta"], {"namespace": NS, "sender_session": "inb-send", "message_id": live["id"]})
    assert s == 200 and r["recalled"] is False and r["already_acted"] is True
    # not found / not owned
    s, _ = call("/inbox/recall", env["tb"], {"namespace": NS, "sender_session": "inb-recv", "message_id": live["id"]})
    assert s == 400
    # thread recall
    t1 = _isend(env, "T1", thread_key="th-r")
    t2 = _isend(env, "T2", thread_key="th-r")
    s, r = call("/inbox/recall", env["ta"], {"namespace": NS, "sender_session": "inb-send", "thread_key": "th-r"})
    assert s == 200 and r["recalled_ids"] == [t1["id"], t2["id"]]
    # pub/sub message recall: never delivered by poll
    s, pm = call("/channel/publish", env["ta"], {"namespace": NS, "channel": "@inb-recv",
                                                 "sender_session": "inb-send", "content": "CHAN-RECALLED"})
    s, r = call("/inbox/recall", env["ta"], {"namespace": NS, "sender_session": "inb-send",
                                             "channel_message_id": pm["id"], "reason": "oops"})
    assert s == 200 and r["recalled"] is True and r["channel_message_id"] == pm["id"]
    s, r = call("/channel/poll", env["tb"], {"namespace": NS, "session_name": "inb-recv", "peek": True})
    assert "CHAN-RECALLED" not in json.dumps(r)
    s, r = call("/inbox/recall", env["tb"], {"namespace": NS, "sender_session": "inb-recv",
                                             "channel_message_id": pm["id"]})
    assert s == 200 and r["recalled"] is False   # not the owner


def test_24_inbox_act_reply_sdk_and_cli(env):
    from memnos_sdk import MemnosClient
    _inbox_sessions(env)
    m = _isend(env, "please review", subject="Review PR", thread_key="th-pr", intent="review_request")
    s, r = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv", "message_id": m["id"],
                                          "action": "in_progress"})
    assert r["item"]["status"] == "in_progress" and r["item"]["acted_at"] is None
    s, r = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv", "message_id": m["id"],
                                          "action": "done", "reply": "LGTM"})
    assert r["ok"] and r["reply_item"]["subject"] == "Re: Review PR" and r["reply_item"]["intent"] == "answer"
    assert r["reply_item"]["in_reply_to"] == m["id"] and r["reply_item"]["thread_key"] == "th-pr"
    back = _ilist(env, tok=env["ta"], to="inb-send")["items"]
    assert any(x["id"] == r["reply_item"]["id"] and x["content"] == "LGTM" for x in back)
    s, _ = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv", "message_id": m["id"], "action": "bogus"})
    assert s == 400
    s, _ = call("/inbox/act", env["tb"], {"namespace": NS, "to_session": "inb-recv", "message_id": 10**9})
    assert s == 400
    # SDK
    with MemnosClient(base_url=URL, token=env["tb"], namespace=NS) as mem:
        it = mem.inbox_send("inb-send", "sdk hello", sender_session="inb-recv", priority="low")
        assert it["status"] == "pending"
        assert mem.inbox_recall(sender_session="inb-recv", message_id=it["id"])["recalled"]
        mine = _isend(env, "for sdk")
        assert any(x["id"] == mine["id"] for x in mem.inbox_list("inb-recv")["items"])
        assert mem.inbox_act("inb-recv", mine["id"], action="read")["item"]["status"] == "read"
    # CLI (runs as the tb principal / inb-recv)
    r = _cli(["inbox", "send", "inb-send", "cli msg", "--priority", "urgent", "--session", "inb-recv",
              "--namespace", NS], env)
    assert r.returncode == 0 and "queued #" in r.stdout, r.stdout + r.stderr
    cid = int(r.stdout.split("#")[1].split()[0])
    r = _cli(["inbox", "recall", str(cid), "--reason", "cli", "--session", "inb-recv", "--namespace", NS], env)
    assert r.returncode == 0 and "recalled" in r.stdout, r.stdout + r.stderr
    cm = _isend(env, "CLI-ACT-MARKER")
    r = _cli(["inbox", "ls", "--session", "inb-recv", "--namespace", NS], env)
    assert f"#{cm['id']}" in r.stdout and "CLI-ACT-MARKER" in r.stdout, r.stdout + r.stderr
    r = _cli(["inbox", "act", str(cm["id"]), "--reply", "done!", "--session", "inb-recv", "--namespace", NS], env)
    assert r.returncode == 0 and "acted" in r.stdout and "reply sent" in r.stdout, r.stdout + r.stderr
