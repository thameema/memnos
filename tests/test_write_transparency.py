"""No-AI tests for write-side namespace transparency (issue #20, Part B).

SUGGEST, NEVER AUTO-ROUTE: a write always lands in the resolved namespace; the system
only makes the destination VISIBLE and a correction one step away. This suite proves:

  - resolve_with_source returns the right SOURCE for each ladder rung
    (explicit / binding_repo / binding_host_repo / binding_host_path / legacy / env / default).
  - /remember echoes the destination `namespace` in its response.
  - default-fallback path -> the warning/bind-offer signal; a bound path -> none.
  - suggest-on-mismatch: with entities seeded into namespace B, a write whose entities are
    B's returns suggestion->B; a write about A's own entities returns NO suggestion.
  - NEVER auto-route: the write still lands in the resolved namespace even when a
    suggestion fires.

HOW THE SUGGESTION IS TESTED (no LLM, $0): extraction needs an LLM, so we DON'T run it.
We seed `entities` rows directly (the exact signal the encoder writes) and call the
server-side helper `Control.suggest_namespace` with a synthetic entity list — the same
direct-seed approach as test_recall_entity_scope.py. The /remember HTTP path is exercised
in local mode (no OPENAI_API_KEY -> LLM is None -> no extraction) to prove the response
echoes the namespace and the write lands where resolved.

Runs against a live local server (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_write_transparency.py
No subprocess, no DB creation — talks to the already-running server + DB, scoped to a
scratch principal + scratch namespaces (test:wt-a/b) that it cleans up afterward.
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------------------
def test_resolve_with_source():
    print("=== resolve_with_source: SOURCE for each ladder rung ===")
    import nsresolve
    tmp = tempfile.mkdtemp(prefix="wt_res_")
    nsresolve._DIR = tmp
    nsresolve._CACHE = os.path.join(tmp, "bindings_cache.json")
    nsresolve._OVR = os.path.join(tmp, "ns_overrides.json")
    nsresolve._MID = os.path.join(tmp, "machine_id")
    mid = nsresolve.machine_id()
    work = tempfile.mkdtemp(prefix="wt_work_")
    rkey = "github.com/test/transparency"
    orig = nsresolve.repo_key
    nsresolve.repo_key = lambda cwd=None: rkey
    os.environ.pop("MEMNOS_NS", None)

    def write_cache(binds):
        json.dump({"bindings": binds, "fetched_at": 0}, open(nsresolve._CACHE, "w"))

    # explicit
    ns, src = nsresolve.resolve_with_source({"cwd": work, "namespace": "ns:explicit"})
    check("explicit arg -> source 'explicit'", ns == "ns:explicit" and src == "explicit")

    # binding_repo (host-agnostic)
    write_cache([{"key_type": "repo", "key": rkey, "namespace": "ns:repo"}])
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("repo cache binding -> source 'binding_repo'", ns == "ns:repo" and src == "binding_repo")

    # binding_host_repo
    write_cache([{"key_type": "host_repo", "key": rkey, "host_id": mid, "namespace": "ns:hr"}])
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("host_repo cache binding -> source 'binding_host_repo'",
          ns == "ns:hr" and src == "binding_host_repo")

    # binding_host_path (no repo)
    nsresolve.repo_key = lambda cwd=None: None
    write_cache([{"key_type": "host_path", "key": work, "host_id": mid, "namespace": "ns:hp"}])
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("host_path cache binding -> source 'binding_host_path'",
          ns == "ns:hp" and src == "binding_host_path")

    # legacy
    write_cache([])
    json.dump({work: "ns:legacy"}, open(nsresolve._OVR, "w"))
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("legacy ns_overrides.json -> source 'legacy'", ns == "ns:legacy" and src == "legacy")

    # env
    os.remove(nsresolve._OVR)
    os.environ["MEMNOS_NS"] = "ns:env"
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("MEMNOS_NS -> source 'env'", ns == "ns:env" and src == "env")

    # default (NO binding -> the fallback signal)
    os.environ.pop("MEMNOS_NS", None)
    ns, src = nsresolve.resolve_with_source({"cwd": work})
    check("nothing bound -> source 'default' + proj:<cwd>",
          src == "default" and ns == "proj:" + os.path.basename(work))

    # the surfacing helpers used by hook/MCP/CLI
    hint = nsresolve.default_fallback_hint(ns, {"cwd": work})
    check("default_fallback_hint warns + offers a copy-pasteable bind",
          "no namespace bound" in hint and "memnos bind " in hint and ns in hint)

    # resolve() back-compat returns just the namespace
    check("resolve() back-compat == resolve_with_source()[0]",
          nsresolve.resolve({"cwd": work}) == ns)
    nsresolve.repo_key = orig


def test_session_dedupe():
    print("=== hook surfacing fires ONCE per session, again only on CHANGE ===")
    import nsresolve
    tmp = tempfile.mkdtemp(prefix="wt_sess_")
    nsresolve._DIR = tmp
    sid = "sess-XYZ"
    first = nsresolve.session_first_time(sid, "proj:a")
    again = nsresolve.session_first_time(sid, "proj:a")
    check("first time for (session, ns) -> True", first is True)
    check("same (session, ns) again -> False (NOT every turn)", again is False)
    changed = nsresolve.session_first_time(sid, "proj:b")
    check("namespace CHANGE in same session -> True again", changed is True)
    back = nsresolve.session_first_time(sid, "proj:a")
    check("after a change, the prior ns surfaces once more", back is True)
    other = nsresolve.session_first_time("sess-OTHER", "proj:a")
    check("a different session surfaces independently", other is True)


def test_suggestion_helper(conn, pid, a_ns, b_ns):
    print("=== suggest-on-mismatch helper (entities seeded directly, $0) ===")
    # seed B's entity world; A stays empty of these names
    for name in ("Gateway", "Nimbus", "Falcon", "Crosswalk"):
        conn.execute(f"INSERT INTO {SCHEMA}.entities(namespace,name) VALUES(%s,%s) "
                     f"ON CONFLICT DO NOTHING", (b_ns, name))

    # WRITE resolved to A, but its entities are all B's -> suggest B
    sugg = Control.suggest_namespace(conn, pid, a_ns,
                                     ["Gateway", "Nimbus", "Falcon"], schema=SCHEMA)
    check("entities that live in B -> suggestion points to B",
          isinstance(sugg, dict) and sugg.get("namespace") == b_ns)
    check("suggestion carries a human reason", sugg and "entities" in sugg.get("reason", ""))

    # WRITE about A's OWN entities -> NO suggestion
    conn.execute(f"INSERT INTO {SCHEMA}.entities(namespace,name) VALUES(%s,%s) "
                 f"ON CONFLICT DO NOTHING", (a_ns, "Borealis"))
    conn.execute(f"INSERT INTO {SCHEMA}.entities(namespace,name) VALUES(%s,%s) "
                 f"ON CONFLICT DO NOTHING", (a_ns, "Aurora"))
    none = Control.suggest_namespace(conn, pid, a_ns, ["Borealis", "Aurora"], schema=SCHEMA)
    check("a write about A's OWN entities -> NO suggestion", none is None)

    # below the min-entity floor -> never suggest (avoids noise on tiny writes)
    check("single-entity write -> no suggestion (below floor)",
          Control.suggest_namespace(conn, pid, a_ns, ["Gateway"], schema=SCHEMA) is None)

    # toggle: an env-gated OFF state is honoured by the server wrapper (helper itself is
    # always callable; the wrapper _write_suggestion respects MEMNOS_SUGGEST_NAMESPACE).
    # Tested at the function level (no server process) by flipping the env around direct
    # calls to _suggest_enabled() / _write_suggestion().
    import memnos_server
    _saved = os.environ.get("MEMNOS_SUGGEST_NAMESPACE")
    os.environ["MEMNOS_SUGGEST_NAMESPACE"] = "0"
    try:
        check("MEMNOS_SUGGEST_NAMESPACE=0 -> _suggest_enabled() False",
              memnos_server._suggest_enabled() is False)
        check("MEMNOS_SUGGEST_NAMESPACE=0 disables the server wrapper",
              memnos_server._write_suggestion(conn, pid, a_ns,
                                              [{"subject": "Gateway"}], "Gateway Nimbus") is None)
        os.environ["MEMNOS_SUGGEST_NAMESPACE"] = "1"
        check("MEMNOS_SUGGEST_NAMESPACE=1 -> _suggest_enabled() True",
              memnos_server._suggest_enabled() is True)
    finally:
        if _saved is None:
            os.environ.pop("MEMNOS_SUGGEST_NAMESPACE", None)
        else:
            os.environ["MEMNOS_SUGGEST_NAMESPACE"] = _saved
    # back ON: wrapper gathers fact-subjects + NER and suggests B
    w = memnos_server._write_suggestion(conn, pid, a_ns,
                                        [{"subject": "Gateway"}, {"subject": "Nimbus"}],
                                        "We discussed Falcon today.")
    check("wrapper ON: subjects+NER from a B-flavored write -> suggests B",
          isinstance(w, dict) and w.get("namespace") == b_ns)


def test_remember_echoes_ns_and_lands(conn, pid, tok, a_ns, b_ns):
    print("=== /remember echoes destination + NEVER auto-routes (local mode) ===")
    # local mode (no LLM): /remember stores the raw turn, no extraction, echoes namespace.
    s, j = call("POST", "/remember", tok, {"namespace": a_ns, "text":
        "We deployed the Gateway service and discussed Nimbus and Falcon at length today."})
    check("/remember 200", s == 200)
    check("/remember response echoes the destination namespace", j.get("namespace") == a_ns)
    tid = j.get("turn_id")

    # NEVER AUTO-ROUTE: the raw turn must be in A (the resolved ns), regardless of any
    # suggestion. (B holds Gateway/Nimbus/Falcon from the helper test, so a suggestion
    # WOULD point to B if extraction ran — but the write still lands in A.)
    row = conn.execute(f"SELECT namespace FROM {SCHEMA}.raw_turns WHERE id=%s", (tid,)).fetchone()
    check("the write LANDED in the resolved namespace (A), never rerouted",
          row and row["namespace"] == a_ns)
    none_in_b = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns WHERE id=%s AND namespace=%s",
                             (tid, b_ns)).fetchone()
    check("the write did NOT land in the suggested namespace (B)", none_in_b["n"] == 0)


def test_remember_suggests_on_mismatch_live(conn, pid, tok, a_ns, b_ns):
    """END-TO-END (the test the prior suite missed): the suggestion must appear in the LIVE
    /remember HTTP RESPONSE on a genuine mismatch, and be ABSENT on a match — proving the
    local-mode wiring + token normalization, not just the helper in isolation.

    DETERMINISTIC AT $0 / local-384: extraction needs an LLM (LLM is None here), so we DON'T
    rely on facts. We seed B's `entities` directly (the exact signal the encoder/fact path
    writes) — half as PHRASES ("Project Zephyr") to prove token-vs-phrase normalization, half
    as TOKENS — then the /remember advisory runs off raw-turn proper-noun NER alone, which is
    fully deterministic (regex), so no LLM and no flakiness."""
    # seed B's entity world — mix of phrase-form (fact-subject style) and token-form (encoder
    # style) to exercise the token normalization on BOTH stored shapes.
    for name in ("Project Zephyr", "Acme Payments", "Falcon"):
        conn.execute(f"INSERT INTO {SCHEMA}.entities(namespace,name) VALUES(%s,%s) "
                     f"ON CONFLICT DO NOTHING", (b_ns, name))
    # A holds its own, unrelated entities (so the control write matches A, not B).
    for name in ("Borealis", "Aurora"):
        conn.execute(f"INSERT INTO {SCHEMA}.entities(namespace,name) VALUES(%s,%s) "
                     f"ON CONFLICT DO NOTHING", (a_ns, name))

    # MISMATCH: write B-flavored text to A -> live response must SUGGEST B.
    s, j = call("POST", "/remember", tok, {"namespace": a_ns, "text":
        "Project Zephyr and the Acme Payments rollout with Falcon slipped to next week."})
    check("/remember (mismatch) 200", s == 200)
    sugg = j.get("suggestion")
    check("LIVE /remember response carries a suggestion on a B-mismatch",
          isinstance(sugg, dict))
    check("the live suggestion points at B", sugg and sugg.get("namespace") == b_ns)
    # GUARDRAIL: it landed in A, never rerouted to B.
    tid = j.get("turn_id")
    row = conn.execute(f"SELECT namespace FROM {SCHEMA}.raw_turns WHERE id=%s", (tid,)).fetchone()
    check("mismatch write LANDED in A (suggest only, never auto-route)",
          row and row["namespace"] == a_ns)
    in_b = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns WHERE id=%s AND namespace=%s",
                        (tid, b_ns)).fetchone()
    check("mismatch write did NOT land in suggested B", in_b["n"] == 0)

    # CONTROL: write A's OWN content to A -> NO suggestion in the live response.
    s2, j2 = call("POST", "/remember", tok, {"namespace": a_ns, "text":
        "Borealis and Aurora reviewed the plan today."})
    check("/remember (match) 200", s2 == 200)
    check("LIVE /remember response has NO suggestion when content matches its own ns",
          j2.get("suggestion") is None)
    tid2 = j2.get("turn_id")
    row2 = conn.execute(f"SELECT namespace FROM {SCHEMA}.raw_turns WHERE id=%s", (tid2,)).fetchone()
    check("control write LANDED in A", row2 and row2["namespace"] == a_ns)


# ---------------------------------------------------------------------------
def main():
    # resolver + dedupe need no server/DB
    test_resolve_with_source()
    test_session_dedupe()

    # Everything else talks to the ALREADY-RUNNING server (URL) + DB (DSN), like the rest
    # of tests/. The server runs in free local-384 mode in CI (no OPENAI_API_KEY -> no
    # extraction), so /remember just stores the raw turn and echoes the namespace — $0.
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    pid = Control.create_principal(conn, "test-wt", "agent")
    tok = Control.mint_token(conn, pid, "test")
    a_ns, b_ns = "test:wt-a", "test:wt-b"
    Control.grant(conn, pid, a_ns)
    Control.grant(conn, pid, b_ns)
    # clean any prior scratch rows
    for ns in (a_ns, b_ns):
        conn.execute(f"DELETE FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
        conn.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
    try:
        test_suggestion_helper(conn, pid, a_ns, b_ns)
        test_remember_echoes_ns_and_lands(conn, pid, tok, a_ns, b_ns)
        test_remember_suggests_on_mismatch_live(conn, pid, tok, a_ns, b_ns)
    finally:
        for ns in (a_ns, b_ns):
            conn.execute(f"DELETE FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
            conn.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
        conn.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
        conn.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
        conn.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
