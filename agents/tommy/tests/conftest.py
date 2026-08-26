"""
Shared fixtures for the DB-backed Secret Shield acceptance tests (issue
#115): test_secret_shield_e2e.py and test_secret_shield_ingest_path.py.

Both need a REAL memnos server (for POST /secret/resolve and POST
/ingest/file) and the REAL Postgres it's backed by (to seed
principals/grants/vault entries directly, and to read back what actually
got stored) — mocking either would defeat the point of an acceptance test
whose entire job is proving real server-side behavior (fail-closed
resolution over the network, ingest-time redaction gaps).

Per this task's isolation requirements: point these at a throwaway
pgvector/pgvector:pg16 container via MEMNOS_DSN/MEMNOS_URL, with
MEMNOS_SECRET_KEY set explicitly and OPENAI_API_KEY explicitly unset on the
SERVER process — never the shared dev Postgres. Also start that server
process with an isolated HOME: memnos_server.py's _load_config() reads
~/.memnos/config.json and does os.environ.setdefault("OPENAI_API_KEY",
cfg["openai"]) if that file has an "openai" key (commonly a literal
"secret://openai"-style placeholder in a real dev setup) — picking that up
would silently steer the test server away from free local-384 embedding
mode and into a broken OpenAI-key code path. This module's fixtures don't
start the server themselves (see the "Run locally" section below for how
to), so this note is aimed at whoever launches it, human or CI.

If no live server is reachable, or its Vault is locked (no
MEMNOS_SECRET_KEY on the server side), these tests SKIP with a clear message
by default (matching tests/test_secret_resolve.py's existing convention) --
UNLESS TOMMY_REQUIRE_SECRET_SHIELD=1 is set, which turns that skip into a
hard failure. This mirrors .github/workflows/ci.yml's existing
MEMNOS_REQUIRE_OMNIGENT convention: a skip and a pass are indistinguishable
from the outside, so CI sets the "require" flag to make sure these
acceptance criteria are actually exercised on every run rather than quietly
skipping forever.

Run locally, against a throwaway container:
    docker run -d --rm -e POSTGRES_USER=memnos -e POSTGRES_PASSWORD=memnos \\
        -e POSTGRES_DB=memnos -p 55901:5432 pgvector/pgvector:pg16
    PGPASSWORD=memnos psql -h localhost -p 55901 -U memnos -d memnos \\
        -c "CREATE EXTENSION IF NOT EXISTS vector"

    env -i HOME=/tmp/memnos-ss-home PATH="$PATH" \\
        MEMNOS_DSN=postgresql://memnos:memnos@localhost:55901/memnos \\
        MEMNOS_SECRET_KEY=$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())") \\
        python3 memnos_server.py &

    MEMNOS_DSN=postgresql://memnos:memnos@localhost:55901/memnos \\
    MEMNOS_URL=http://127.0.0.1:8900 \\
    python -m pytest agents/tommy/tests/test_secret_shield_e2e.py \\
                      agents/tommy/tests/test_secret_shield_ingest_path.py -q
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# psycopg + core.control are repo-root (server-side) dependencies, NOT
# tommy-orchestrator dependencies — the fast `tommy-tests` CI job (pure unit
# tests, agents/tommy/tests -q) never installs them, and this conftest.py is
# collected for EVERY test in this directory, including that job's. A
# top-level import here would break collection for the whole directory on
# that job. Deferred into live_memnos() itself instead; its absence is just
# another reason to skip/fail exactly like an unreachable server.
try:
    import psycopg
    from psycopg.rows import dict_row
    from core.control import Control
    _DB_IMPORT_ERROR = None
except ImportError as exc:
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore
    Control = None  # type: ignore
    _DB_IMPORT_ERROR = exc


@pytest.fixture(autouse=True)
def _isolate_tommy_scope_state(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation for EVERY test in this directory, not
    just the ones that explicitly exercise dispatch-scoping.

    memnos_scope.py's concurrency-safety fix (a blocking finding from an
    adversarial review, post-issue-#136 landing — see its module docstring's
    "Concurrency safety" section) persists a small per-workspace lock/
    refcount/snapshot state file under ~/.memnos/tommy_scope/ whenever
    should_scope_dispatch() activates during a real tommy_dispatch()/
    _launch_harness() call. Several PRE-EXISTING dispatch tests in this
    suite (e.g. test_corpus_gate.py's Layer 2, test_verdict.py,
    test_dispatch_core_prompt_parity.py, test_reviewer_dispatch_passes.py)
    write a tommy.yaml with an explicit memnos.namespace and call
    tommy_dispatch()/_launch_harness() IN-PROCESS as part of testing
    something else entirely (corpus gating, verdict diffing, prompt parity)
    — on any dev machine where a real `memnos` binary happens to be on
    PATH and the workspace has no existing binding (true for a fresh
    tmp_path almost by construction), should_scope_dispatch() activates
    for them too, incidentally. Before this fixture, every one of those
    tests silently wrote a stray lockfile into whoever's REAL $HOME ran the
    suite. Autouse here means no test — present or future — has to
    remember to opt in just to avoid that.

    Subprocess-based driver tests (spawning a real separate Python process)
    are unaffected by this monkeypatch — it only patches the PARENT pytest
    process's own `tommy.memnos_scope` import, never a child process's
    fresh one — those isolate via their own $HOME env var instead (see
    test_cli_dispatch.py's fake_home / fixtures/sigint_driver.py's _Rig-
    style setup, both pre-existing conventions this fixture doesn't
    change)."""
    try:
        import tommy.memnos_scope as ms
    except Exception:
        return
    # Deliberately NOT raising=False: if a future refactor renames
    # _SCOPE_STATE_DIR, this should fail collection loudly rather than
    # silently stop isolating and resume writing into the real $HOME.
    monkeypatch.setattr(ms, "_SCOPE_STATE_DIR", tmp_path / "_tommy_scope_state_isolated")

# Deliberately NOT defaulting to "conventional" localhost:5432 / 127.0.0.1:8900
# the way tests/test_secret_resolve.py does — this repo's own dev workflow
# routinely leaves a REAL native Postgres on 5432 and a REAL long-running
# memnos server on 8900 for day-to-day work, and those conventional ports
# are exactly what an unset env var would silently fall through to. This
# was hit for real while writing these tests: running the suite without
# explicitly exporting MEMNOS_DSN/MEMNOS_URL connected straight to that live
# setup instead of skipping (cleanly torn down afterward, but it should
# never have connected at all). Requiring both to be explicitly set — no
# default — is the fix: a throwaway pgvector/pgvector:pg16 container is
# opt-in, never accidental.
DSN = os.environ.get("MEMNOS_DSN")
URL = os.environ.get("MEMNOS_URL")
REQUIRE = os.environ.get("TOMMY_REQUIRE_SECRET_SHIELD", "") == "1"
SCHEMA = "tenant_memnos"


def call(method, path, token=None, body=None, timeout=10):
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})},
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}
    except urllib.error.URLError:
        return None, {}


def _skip_or_fail(reason: str):
    if REQUIRE:
        pytest.fail(f"{reason} (TOMMY_REQUIRE_SECRET_SHIELD=1 set -- this must run, not skip)")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def live_memnos():
    """A real, reachable memnos server + Postgres with an admin principal
    already minted. Skips (or fails — see module docstring) if the server
    is unreachable or its Vault is locked."""
    if _DB_IMPORT_ERROR is not None:
        _skip_or_fail(f"psycopg / core.control not importable ({_DB_IMPORT_ERROR}) — "
                      "run `pip install -r requirements.txt` from the repo root")
    if not DSN or not URL:
        _skip_or_fail(
            "MEMNOS_DSN and MEMNOS_URL must both be explicitly set (no default — see "
            "module docstring for why) to run this test against a real memnos server"
        )

    status, _ = call("GET", "/healthz")
    if status != 200:
        _skip_or_fail(f"memnos server not reachable at {URL}")

    try:
        conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    except Exception as exc:
        _skip_or_fail(f"cannot connect to Postgres at {DSN}: {exc}")
        return  # pragma: no cover — _skip_or_fail always raises
    Control.init(conn)

    run = f"{int(time.time() * 1000)}_{os.getpid()}"
    admin_id = Control.create_principal(conn, f"test_ss_admin_{run}", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test-secret-shield-admin")
    principal_ids = [admin_id]

    st, sec = call("GET", "/admin/api/secrets", admin_tok)
    if st != 200 or not sec.get("unlocked"):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (admin_id,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (admin_id,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (admin_id,))
        conn.close()
        _skip_or_fail("memnos server's Vault is locked (no MEMNOS_SECRET_KEY set on the server process)")

    _counter = {"n": 0}

    def make_principal(kind: str = "agent") -> int:
        _counter["n"] += 1
        pid = Control.create_principal(conn, f"test_ss_{kind}_{run}_{_counter['n']}", kind)
        principal_ids.append(pid)
        return pid

    def cleanup_namespace(namespace: str) -> None:
        with conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (namespace,))
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (namespace,))

    yield {
        "conn": conn,
        "run": run,
        "url": URL,
        "dsn": DSN,
        "admin_id": admin_id,
        "admin_tok": admin_tok,
        "call": call,
        "make_principal": make_principal,
        "cleanup_namespace": cleanup_namespace,
    }

    with conn.cursor() as c:
        for pid in principal_ids:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))
    conn.close()
