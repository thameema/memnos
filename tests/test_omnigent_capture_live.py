"""REAL end-to-end proof for the Omnigent capture policy — a genuine Omnigent-shaped
RESPONSE-phase event, fed straight into `capture_response`, verified recallable from a
REAL running memnos server backed by REAL Postgres. No mocks anywhere in this file.

This exists because a prior claim of "verified live integration" for a related memnos<->
Omnigent feature turned out to be unreproducible (no log file existed on disk, no agent
had actually been run) — see the memnos-omnigent audit trail. The bar for THIS PR is: an
independent reader can run this file, watch it fail if anything is wrong, and see the
recalled memory's exact content printed as proof.

What this test does NOT prove: that Omnigent's own `resolve_function_policy` /
`_build_event()` actually produce this exact dict at runtime inside a real `omnigent
server` process — that requires the separate, out-of-scope omnigent repo and its own
test/CI, and isn't something this repo can execute. What it establishes instead: (a) the
event dict shape below is transcribed from a direct reading of omnigent/policies/
function.py `_build_event()` and omnigent/server/routes/_sessions/helpers.py
`_evaluate_output_policy()` (see the design notes in sdk/memnos_sdk/integrations/
omnigent.py's module docstring for exact line references), and (b) GIVEN that shape,
`capture_response` really does turn it into a durable, recallable memnos fact.

Requires a live memnos server + Postgres — set MEMNOS_URL / MEMNOS_DSN if not using the
defaults. Mirrors tests/test_memory_api.py's principal/token bootstrap pattern.
Run: python tests/test_omnigent_capture_live.py
"""
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sdk"))

import psycopg
from psycopg.rows import dict_row

from core.control import Control

from memnos_sdk.integrations import omnigent as capture_mod

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:omnigent_capture_live"
SCHEMA = "tenant_memnos"

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def wait_for_server(timeout_s=20):
    # issue #59: /readyz, not /healthz — this gates real /recall and /remember calls
    # below, and /healthz's 200 (liveness only) gives no guarantee the pool/HNSW
    # indexes are actually warm. /readyz does.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL + "/readyz", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def build_response_phase_event(text, *, run_as=None, client_id=None):
    """The event dict Omnigent's FunctionPolicy._call -> _build_event() constructs for
    a RESPONSE-phase (assistant message) evaluation. Transcribed field-for-field from
    omnigent/policies/function.py `_build_event()` (~line 190-253): `data` is the raw
    assistant text (str, per EvaluationContext's own field docs), `target` is always
    None on RESPONSE, and `request_data` is omitted entirely (only present on
    TOOL_RESULT) — this is NOT a simplification, it's the exact shape for this phase."""
    actor = {}
    if run_as is not None:
        actor["run_as"] = run_as
    if client_id is not None:
        actor["client_id"] = client_id
    return {
        "type": "response",
        "target": None,
        "data": text,
        "context": {
            "actor": actor,
            "usage": {},
            "user_daily_cost": {},
            "model": None,
            "harness": None,
            "labels": {},
            "subtree_usage": {},
        },
        "session_state": {},
        "llm_client": None,
    }


def recall_context(token, query, *, retries=8, delay_s=1.0):
    """Poll /recall a few times — async remember returns before the (near-instant,
    local-384-mode) fact extraction necessarily completes; this tolerates slower
    extraction backends (e.g. a real LLM) without a fixed sleep."""
    last = ""
    for _ in range(retries):
        req = urllib.request.Request(
            URL + "/recall", method="POST",
            data=__import__("json").dumps({"namespace": NS, "query": query,
                                           "raw_quota": 5, "fact_quota": 5}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            body = __import__("json").loads(resp.read())
        except urllib.error.HTTPError as e:
            body = __import__("json").loads(e.read() or b"{}")
        last = body.get("context", "")
        if last:
            return last, body
        time.sleep(delay_s)
    return last, {}


def main():
    print("=== omnigent capture policy: REAL event -> REAL memnos server ===")

    if not wait_for_server():
        print(f"FAIL  memnos server not ready at {URL}/readyz within 20s — "
              f"start it first (`memnos start` or `python memnos_server.py`) and re-run.")
        sys.exit(1)
    check(f"memnos server reachable at {URL}", True)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))

    pid = Control.create_principal(conn, "test-omnigent-capture-live", "agent")
    Control.grant(conn, pid, NS, can_read=True, can_write=True)
    token = Control.mint_token(conn, pid, "test")

    ASSISTANT_TEXT = ("The checkout service staging deploy finished successfully using "
                      "image tag release candidate seven, verified by the smoke suite.")

    # --- the actual entry point Omnigent would call, with a real event dict ---
    os.environ["MEMNOS_TOKEN"] = token
    event = build_response_phase_event(ASSISTANT_TEXT, run_as="omnigent-test-agent")
    result = capture_mod.capture_response(event, {"memnos_url": URL, "memnos_namespace": NS})
    check("capture_response returns the ALLOW verdict Omnigent's engine requires",
          result == {"result": "allow"})

    thread = capture_mod._last_capture_thread
    check("capture_response spawned its background write thread", thread is not None)
    if thread is not None:
        thread.join(timeout=15)
        check("background write completed (didn't hang)", not thread.is_alive())

    context, body = recall_context(token, "checkout service staging deploy")
    check("the captured assistant response is recallable from a REAL memnos server",
          "checkout" in context.lower() and "staging" in context.lower())
    print(f"  -> recalled context: {context!r}")
    check("recalled as speaker=assistant (correct attribution, not mislabeled as user)",
          any(m.get("content", "") for m in body.get("memories", []))
          and _stored_speaker_for_text(conn, NS, "checkout service staging deploy") == "assistant")

    # --- never-raise guarantee, exercised against the SAME live server: a bad namespace/
    # URL combination must still return ALLOW, not raise, not touch the real namespace ---
    os.environ["MEMNOS_TOKEN"] = "mnk_definitely_not_a_real_token"
    bad_event = build_response_phase_event("this write should fail closed but never raise")
    try:
        bad_result = capture_mod.capture_response(bad_event, {"memnos_url": URL, "memnos_namespace": NS})
        raised = False
    except Exception:
        bad_result, raised = None, True
    check("an unauthorized write still returns ALLOW (never raises, never DENYs the real turn)",
          not raised and bad_result == {"result": "allow"})
    if capture_mod._last_capture_thread is not None:
        capture_mod._last_capture_thread.join(timeout=15)

    # cleanup
    os.environ.pop("MEMNOS_TOKEN", None)
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
    conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


def _stored_speaker_for_text(conn, ns, text_fragment):
    """Look up the speaker of the specific raw turn containing `text_fragment` — not
    just "whatever's newest", so this can't pass by coincidentally matching an
    unrelated later write in the same namespace."""
    with conn.cursor() as c:
        c.execute(f"SELECT speaker FROM {SCHEMA}.raw_turns WHERE namespace=%s "
                  f"AND text LIKE %s ORDER BY id DESC LIMIT 1",
                 (ns, f"%{text_fragment}%"))
        row = c.fetchone()
        return row["speaker"] if row else None


if __name__ == "__main__":
    main()
