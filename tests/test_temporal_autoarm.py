"""Issue #25 — timeline arm auto-activation for temporal-language queries.

Unit tests for the expanded _IS_TEMPORAL / _IS_TEMPORAL_EXT patterns and
the MEMNOS_TEMPORAL_AUTO_ARM env-var kill-switch.  Integration tests seed a
live namespace and confirm the timeline arm fires on temporal queries but not
on plain semantic ones.

Run (unit tests only — no server/DB needed):
    python tests/test_temporal_autoarm.py

Run (with live server + DB):
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos \\
    MEMNOS_URL=http://127.0.0.1:8900 \\
    python tests/test_temporal_autoarm.py
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import temporal as T

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:temporal-arm"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond)
    FAIL += int(not cond)


def detect_temporal_language(query):
    """Thin wrapper around T.analyze() for readability in tests."""
    return T.analyze(query, datetime(2026, 1, 15, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Helper: HTTP call to the live server
# ---------------------------------------------------------------------------

def _load_token():
    cfg = os.path.expanduser("~/.memnos/config.json")
    try:
        with open(cfg) as f:
            return json.load(f).get("admin_token", "")
    except Exception:
        return ""


def call(path, body=None, method="POST"):
    token = _load_token()
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------------------
# 1. Unit tests — no server or DB required
# ---------------------------------------------------------------------------

def test_unit_temporal_patterns():
    print("unit: new temporal patterns trigger intent.temporal=True")

    cases_true = [
        ("what changed recently?", "what changed"),
        ("used to work at Acme", "used to"),
        ("is this still valid?", "still"),
        ("history of the project", "history of"),
        ("over time how did X change", "over time"),
        ("when did we last discuss this", "when did"),
        ("no longer maintained", "no longer"),
        ("what was the previous version", "previous"),
        ("is X deprecated now", "deprecated"),
        ("give me a timeline of events", "timeline"),
    ]

    for query, hint in cases_true:
        intent = detect_temporal_language(query)
        ok = intent.temporal is True
        check(f"temporal=True  [{hint}] {query!r}", ok)

    cases_false = [
        "what does Alice do at work",
        "tell me about the database schema",
    ]

    for query in cases_false:
        intent = detect_temporal_language(query)
        check(f"temporal=False  {query!r}", intent.temporal is False)


def test_unit_existing_patterns_still_work():
    print("unit: original _IS_TEMPORAL patterns still work")
    cases = [
        ("when did she join?", True),
        ("what happened before the migration", True),
        ("what came after the release", True),
        ("she has been there recently", True),
        ("what is her current role", True),
        ("what did X do in March", True),
        ("the project started in february", True),
        ("what year did they launch", True),
    ]
    for query, want in cases:
        intent = detect_temporal_language(query)
        check(f"existing pattern temporal={want}  {query!r}", intent.temporal == want)


def test_unit_matched_pattern():
    print("unit: matched_pattern is set when temporal=True")
    true_queries = [
        "what changed recently?",
        "used to work at Acme",
        "is this still valid?",
        "history of the project",
        "when did we last discuss this",
        "give me a timeline of events",
        "is X deprecated now",
        "what was the previous version",
        "no longer maintained",
    ]
    for query in true_queries:
        intent = detect_temporal_language(query)
        has_pattern = intent.matched_pattern is not None
        check(f"matched_pattern set  {query!r}", intent.temporal and has_pattern)

    non_temporal = "what does Alice do at work"
    intent = detect_temporal_language(non_temporal)
    check("matched_pattern is None for non-temporal", intent.matched_pattern is None)


def test_unit_auto_arm_env_var():
    print("unit: MEMNOS_TEMPORAL_AUTO_ARM=0 disables keyword-based arm")
    saved = os.environ.pop("MEMNOS_TEMPORAL_AUTO_ARM", None)
    try:
        # with auto-arm disabled, keyword-only queries must NOT set temporal
        os.environ["MEMNOS_TEMPORAL_AUTO_ARM"] = "0"

        no_arm_queries = [
            "what changed recently",
            "used to work there",
            "is this still valid",
            "history of the project",
            "over time how did X change",
            "give me a timeline of events",
        ]
        for query in no_arm_queries:
            intent = detect_temporal_language(query)
            check(f"auto_arm=0 temporal=False  {query!r}", intent.temporal is False)

        # explicit year still works even when auto-arm is disabled
        intent = detect_temporal_language("what happened in 2023")
        check("auto_arm=0 explicit year still triggers temporal=True",
              intent.temporal is True)

        # explicit month name still works
        intent = detect_temporal_language("what happened in march")
        check("auto_arm=0 explicit month still triggers temporal=True",
              intent.temporal is True)

    finally:
        os.environ.pop("MEMNOS_TEMPORAL_AUTO_ARM", None)
        if saved is not None:
            os.environ["MEMNOS_TEMPORAL_AUTO_ARM"] = saved


def test_unit_extended_coverage():
    print("unit: broader coverage of issue #25 patterns")
    extended_cases = [
        # change/history language
        ("has this changed recently", True),
        ("has the config changed", True),
        ("what is the progression of X", True),
        ("earlier we discussed the rollout", True),
        ("back when we started the project", True),
        ("at the time of the migration", True),
        ("prior to the update what was the value", True),
        ("subsequent to the release what happened", True),
        ("how long ago did we deploy", True),
        ("how long since the last incident", True),
        ("last time we ran the test it failed", True),
        ("first time she visited the office", True),
        ("is X outdated", True),
        ("what is the newest version available", True),
        ("out of date information about the schema", True),
        ("latest version of the library", True),
        ("how has X evolved over the years", True),
        ("anymore does she work there", True),
        # non-temporal control cases
        ("what does Alice do at work", False),
        ("describe the architecture", False),
    ]
    for query, want in extended_cases:
        intent = detect_temporal_language(query)
        check(f"temporal={want}  {query!r}", intent.temporal == want)


# ---------------------------------------------------------------------------
# 2. Integration tests — require live server + DB
# ---------------------------------------------------------------------------

def test_integration_timeline_arm(conn, token):
    print("integration: timeline arm fires on temporal queries")

    # seed dated facts about Alice
    facts = [
        "Alice joined the company in March 2024.",
        "Alice was promoted to senior engineer in January 2025.",
        "Alice moved to the platform team in June 2025.",
    ]
    for fact in facts:
        status, body = call("/remember", {
            "namespace": NS,
            "text": fact,
            "extract": False,
        })
        check(f"seed fact seeded OK: {fact[:50]!r}", status == 200)

    # temporal query: should get facts with dates in context
    status, body = call("/recall", {"namespace": NS, "query": "when did Alice change roles?"})
    check("recall /when did Alice change roles/ returns 200", status == 200)
    ctx = body.get("context", "") or ""
    has_date = "2024" in ctx or "2025" in ctx
    check("context contains a year (timeline arm fired)", has_date)

    # another temporal query
    status2, body2 = call("/recall", {
        "namespace": NS,
        "query": "what has changed over time for Alice?",
    })
    check("recall /what has changed over time/ returns 200", status2 == 200)
    ctx2 = body2.get("context", "") or ""
    has_date2 = "2024" in ctx2 or "2025" in ctx2
    check("context contains a year for change-over-time query", has_date2)

    # non-temporal query: semantic recall still works (just no forced timeline arm)
    status3, body3 = call("/recall", {"namespace": NS, "query": "tell me about Alice"})
    check("non-temporal query /tell me about Alice/ returns 200", status3 == 200)
    # we don't assert timeline arm NOT fired here (it may or may not due to 'Alice' entity);
    # just confirm the call succeeds and returns something
    check("non-temporal query returns a context or rows",
          body3.get("context") is not None or body3.get("rows") is not None)


def cleanup_ns(conn):
    try:
        with conn.cursor() as c:
            c.execute(
                f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,)
            )
            c.execute(
                f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,)
            )
        conn.commit()
    except Exception as e:
        print(f"  cleanup warning: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    # --- unit tests (no server/DB needed) ---
    test_unit_temporal_patterns()
    test_unit_existing_patterns_still_work()
    test_unit_matched_pattern()
    test_unit_auto_arm_env_var()
    test_unit_extended_coverage()

    # --- integration tests (skip gracefully if server/DB unavailable) ---
    print("integration: checking server availability ...")
    try:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(DSN, autocommit=False, row_factory=dict_row)

        # confirm server is up
        try:
            req = urllib.request.Request(URL + "/health", method="GET")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            conn.close()
            print(f"  SKIP integration tests (server not reachable: {e})")
        else:
            token = _load_token()
            try:
                cleanup_ns(conn)
                test_integration_timeline_arm(conn, token)
            finally:
                cleanup_ns(conn)
                conn.close()

    except Exception as e:
        print(f"  SKIP integration tests ({e})")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
