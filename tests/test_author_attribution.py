"""No-AI tests for AUTHOR-ATTRIBUTED memory (0.1.6) — every server write stamps
author_principal from the AUTHENTICATED principal's name (token-derived); an `author`
field in the request body is IGNORED (non-spoofable). Recall rows carry `author`; the
context block tags lines '(by <author>)' only when the author differs from the caller.
Also covers the /recall {"author": ...} filter.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_author_attribution.py
"""
import json
import os
import sys

import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
NS = "test:author"
PASS = FAIL = 0


def call(path, token=None, body=None):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for t in ("raw_turns", "semantic", "episodic"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    BrainStore(conn=conn).create_schema("memnos")
    cleanup(conn)

    alice_id = Control.create_principal(conn, "auth-alice", "user")
    alice_tok = Control.mint_token(conn, alice_id, "t")
    bot_id = Control.create_principal(conn, "auth-bot", "agent")
    bot_tok = Control.mint_token(conn, bot_id, "t")
    for pid in (alice_id, bot_id):
        Control.grant(conn, pid, NS, can_read=True, can_write=True)

    print("=== author attribution (server-stamped, non-spoofable) ===")
    # alice writes — and tries to SPOOF the author via the body (must be ignored)
    s, j = call("/remember", alice_tok,
                {"namespace": NS, "text": "alice prefers indigo zibblefruit tea",
                 "author": "evil-spoof"})
    check("alice remember 200", s == 200)
    s, j = call("/remember", bot_tok,
                {"namespace": NS, "text": "the zibblefruit invoice total is 42 dollars"})
    check("bot remember 200", s == 200)

    with conn.cursor() as c:
        c.execute(f"SELECT text, author_principal FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        rows = {r["text"]: r["author_principal"] for r in c.fetchall()}
    check("raw turn stamped with alice's principal name",
          rows.get("alice prefers indigo zibblefruit tea") == "auth-alice")
    check("raw turn stamped with bot's principal name",
          rows.get("the zibblefruit invoice total is 42 dollars") == "auth-bot")
    check("spoofed body author NEVER stored", "evil-spoof" not in rows.values())

    # recall surfaces the author per row
    s, j = call("/recall", alice_tok, {"namespace": NS, "query": "zibblefruit"})
    check("recall 200", s == 200)
    by_content = {m["content"]: m.get("author") for m in j.get("memories", [])}
    check("alice's memory attributed to auth-alice",
          by_content.get("alice prefers indigo zibblefruit tea") == "auth-alice")
    check("bot's memory attributed to auth-bot",
          by_content.get("the zibblefruit invoice total is 42 dollars") == "auth-bot")

    # context tags ONLY non-self authors: alice sees the bot's line tagged, not her own
    ctx = j.get("context", "")
    check("context tags the OTHER principal's line", "by auth-bot" in ctx)
    check("context does NOT tag the caller's own lines", "by auth-alice" not in ctx)
    s, j2 = call("/recall", bot_tok, {"namespace": NS, "query": "zibblefruit"})
    ctx2 = j2.get("context", "")
    check("for the bot, alice's line is the tagged one",
          "by auth-alice" in ctx2 and "by auth-bot" not in ctx2)

    # corpus ingest (semantic constraint write path) is attributed too
    s, j = call("/corpus/ingest", bot_tok,
                {"namespace": NS, "name": "authdoc",
                 "text": "The flurble exporter MUST run daily."})
    check("corpus ingest 200 with constraints", s == 200 and j.get("constraints", 0) >= 1)
    with conn.cursor() as c:
        c.execute(f"SELECT author_principal FROM {SCHEMA}.semantic "
                  f"WHERE namespace=%s AND kind='constraint'", (NS,))
        auth = [r["author_principal"] for r in c.fetchall()]
    check("constraint facts stamped with bot's name", auth and all(a == "auth-bot" for a in auth))

    # FEATURE 3: author filter on /recall
    s, j = call("/recall", alice_tok, {"namespace": NS, "query": "zibblefruit",
                                       "author": "auth-bot"})
    mems = j.get("memories", [])
    check("author filter returns only that author's rows",
          mems and all(m.get("author") == "auth-bot" for m in mems))
    check("author filter excludes other authors' content",
          all("indigo" not in m["content"] for m in mems))
    s, j = call("/recall", alice_tok, {"namespace": NS, "query": "zibblefruit",
                                       "author": "nobody-here"})
    check("unknown author filter yields no memories", j.get("memories") == [])

    cleanup(conn)
    with conn.cursor() as c:
        for pid in (alice_id, bot_id):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
