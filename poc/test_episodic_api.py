"""No-AI tests for the episodic tier + decay. Seeds raw turns across two sessions, segments
them into episodes, recalls an episode by content, fetches it with its turns, checks the
access signal, and verifies decay re-scores salience (recent/accessed > old/untouched).
Segmentation is pure SQL + heuristics; no LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_episodic_api.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from memnos_brain.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:episodic"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.provenance WHERE episodic_id IN (SELECT id FROM {SCHEMA}.episodic WHERE namespace=%s)", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.provenance WHERE semantic_id IN (SELECT id FROM {SCHEMA}.semantic WHERE namespace=%s)", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.mentions m USING {SCHEMA}.entities e WHERE m.entity_id=e.id AND e.namespace=%s", (NS,))
        for t in ("episodic", "semantic", "entities", "raw_turns"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))


def episodes(conn):
    with conn.cursor() as c:
        c.execute(f"SELECT id, session_id, access_count, salience FROM {SCHEMA}.episodic WHERE namespace=%s ORDER BY id", (NS,))
        return c.fetchall()


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    user_id = Control.create_principal(conn, "test-epi-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    ro_id = Control.create_principal(conn, "test-epi-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== episodic tier + decay ===")
    # seed two sessions of raw turns (extract off-path; no LLM needed for segmentation)
    for txt in ["We discussed the Zorblax migration timeline.",
                "Zorblax rollout is set for Q3.",
                "The data team owns Zorblax."]:
        call("POST", "/remember", user_tok, {"namespace": NS, "text": txt, "session_id": "meeting-a"})
    for txt in ["The Quffin budget was approved.",
                "Quffin launches next month."]:
        call("POST", "/remember", user_tok, {"namespace": NS, "text": txt, "session_id": "meeting-b"})

    # ACL
    check("no token -> 401", call("POST", "/episode/segment", None, {"namespace": NS})[0] == 401)
    check("read-only -> 403 on segment", call("POST", "/episode/segment", ro_tok, {"namespace": NS})[0] == 403)

    # segment -> one episode per session
    s, j = call("POST", "/episode/segment", user_tok, {"namespace": NS})
    check("segment 200", s == 200)
    check("two episodes (one per session)", j.get("episodes") == 2)

    eps = episodes(conn)
    epA = next(e["id"] for e in eps if e["session_id"] == "meeting-a")
    epB = next(e["id"] for e in eps if e["session_id"] == "meeting-b")

    # recall an episode by content
    s, j = call("POST", "/episode/recall", user_tok, {"namespace": NS, "query": "Zorblax migration"})
    check("episode recall 200", s == 200)
    top = j.get("episodes", [])
    check("recall surfaces the Zorblax episode", top and "Zorblax" in top[0].get("content", ""))
    check("recall empty query -> 400", call("POST", "/episode/recall", user_tok, {"namespace": NS})[0] == 400)

    # fetch one episode with its turns
    s, j = call("POST", "/episode", user_tok, {"namespace": NS, "id": epA})
    check("get_episode 200", s == 200)
    check("episode carries its 3 turns", len(j.get("turns", [])) == 3)
    check("episode unknown -> 404", call("POST", "/episode", user_tok, {"namespace": NS, "id": 999999999})[0] == 404)

    # access signal recorded (recall + get both touched epA)
    a = next(e for e in episodes(conn) if e["id"] == epA)
    check("access_count incremented on recall/get", a["access_count"] >= 2)

    # decay: age epB, make epA recent+accessed, then re-score
    with conn.cursor() as c:
        c.execute(f"UPDATE {SCHEMA}.episodic SET observed_at=now()-interval '400 days', last_access=NULL, access_count=0 WHERE id=%s", (epB,))
        c.execute(f"UPDATE {SCHEMA}.episodic SET last_access=now(), access_count=5 WHERE id=%s", (epA,))
    s, j = call("POST", "/episode/decay", user_tok, {"namespace": NS})
    check("decay 200 + updated>=2", s == 200 and j.get("updated", 0) >= 2)
    rows = {e["id"]: e["salience"] for e in episodes(conn)}
    check("recent/accessed episode more salient than old one", rows[epA] > rows[epB])
    check("old episode decayed low", rows[epB] < 0.2)

    # incremental/idempotent: nothing new to segment
    s, j = call("POST", "/episode/segment", user_tok, {"namespace": NS})
    check("re-segment yields 0 new episodes", j.get("episodes") == 0)

    cleanup(conn)
    for pid in (user_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
