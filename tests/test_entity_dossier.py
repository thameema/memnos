"""Tests for issue #23: entity dossier generation.

Tests:
  - generate_entity_dossier() with a mocked LLM call
  - Control.entity_dossier_candidates() with seeded data
  - BrainStore store/retrieve dossier via direct DB
  - POST /entity/dossier API endpoint (read + 404)
  - get_entity_dossier MCP tool returns correct text

Run:
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos \
    MEMNOS_URL=http://127.0.0.1:8900 \
    python -m pytest tests/test_entity_dossier.py -v

No LLM spend: generate_entity_dossier is tested with a mock; the
/consolidate hook is not exercised (requires MEMNOS_ENTITY_DOSSIERS=1).
"""
import json
import os
import sys
import types
import urllib.request

import pytest
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.control import Control
from core.service import generate_entity_dossier
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:entity_dossier"
SCHEMA = "tenant_memnos"


# ---------------------------------------------------------------------------
# Shared DB fixtures
# ---------------------------------------------------------------------------

def _connect():
    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)


def _cleanup(conn, store):
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        eids = [r["id"] for r in c.fetchall()]
        if eids:
            c.execute(f"DELETE FROM {SCHEMA}.entity_dossiers WHERE namespace=%s", (NS,))
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "semantic", "entities"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    _cleanup(conn, store)
    yield conn, store
    _cleanup(conn, store)
    conn.close()


# ---------------------------------------------------------------------------
# Helper: seed an entity with mentions
# ---------------------------------------------------------------------------

def _seed_entity(conn, store, entity_name, n_mentions=4):
    """Create an entity and n_mentions semantic facts mentioning it."""
    eid = store.upsert_entity(SCHEMA, NS, entity_name)
    fact_ids = []
    for i in range(n_mentions):
        fid = store.insert_semantic(
            SCHEMA, NS, "fact",
            f"{entity_name} is associated with fact number {i + 1}.",
            subject=entity_name, predicate="associated_with", obj=f"fact_{i + 1}")
        store.add_mention(SCHEMA, eid, fid, "semantic")
        fact_ids.append(fid)
    return eid, fact_ids


# ---------------------------------------------------------------------------
# Test: generate_entity_dossier with mocked LLM (no API spend)
# ---------------------------------------------------------------------------

class _FakeChoice:
    def __init__(self, text):
        self.message = types.SimpleNamespace(content=text)


class _FakeLLM:
    """Minimal stub satisfying the llm.chat.completions.create() call pattern."""
    def __init__(self, response_text):
        self._text = response_text
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return types.SimpleNamespace(choices=[_FakeChoice(self._text)])


def test_generate_entity_dossier_returns_text():
    llm = _FakeLLM("Alice is a software engineer who specialises in distributed systems.")
    result = generate_entity_dossier("Alice", ["Alice works at Acme.", "Alice likes Python."],
                                     llm, "test-model")
    assert "Alice" in result
    assert len(result) >= 10


def test_generate_entity_dossier_empty_facts_returns_empty():
    llm = _FakeLLM("should not be called")
    result = generate_entity_dossier("Bob", [], llm, "test-model")
    assert result == ""


def test_generate_entity_dossier_none_llm_returns_empty():
    result = generate_entity_dossier("Carol", ["Carol does things."], None, "test-model")
    assert result == ""


def test_generate_entity_dossier_llm_exception_returns_empty():
    class _BrokenLLM:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("pretend network error")
    result = generate_entity_dossier("Dave", ["Dave does things."], _BrokenLLM(), "test-model")
    assert result == ""


def test_generate_entity_dossier_no_emdash_in_prompt():
    """Verify the prompt itself contains no em-dash characters (issue #23 constraint)."""
    import inspect
    src = inspect.getsource(generate_entity_dossier)
    assert "—" not in src, "em-dash found in generate_entity_dossier source"


# ---------------------------------------------------------------------------
# Test: Control.entity_dossier_candidates
# ---------------------------------------------------------------------------

def test_entity_dossier_candidates_basic(db):
    conn, store = db
    # Seed: Alice gets 4 mentions (above threshold), Bob gets 2 (below)
    alice_id, _ = _seed_entity(conn, store, "Alice_Cand", n_mentions=4)
    bob_id, _ = _seed_entity(conn, store, "Bob_Cand", n_mentions=2)

    cands = Control.entity_dossier_candidates(conn, SCHEMA, NS, min_mentions=3)
    names = [c["name"] for c in cands]
    assert "Alice_Cand" in names, "Alice_Cand (4 mentions) should be a candidate"
    assert "Bob_Cand" not in names, "Bob_Cand (2 mentions) should be below threshold"


def test_entity_dossier_candidates_returns_required_keys(db):
    conn, store = db
    cands = Control.entity_dossier_candidates(conn, SCHEMA, NS, min_mentions=3)
    if cands:
        c = cands[0]
        assert "entity_id" in c
        assert "name" in c
        assert "mention_count" in c
        assert c["mention_count"] >= 3


def test_entity_dossier_candidates_empty_namespace(db):
    conn, store = db
    result = Control.entity_dossier_candidates(conn, SCHEMA, "test:no_such_ns_xyz", min_mentions=3)
    assert result == []


# ---------------------------------------------------------------------------
# Test: BrainStore store + retrieve dossier
# ---------------------------------------------------------------------------

def test_store_and_retrieve_dossier(db):
    conn, store = db
    eid = store.upsert_entity(SCHEMA, NS, "Dossier_Entity")
    text = "Dossier_Entity is a key actor in the memnos test suite."
    row_id = store.store_entity_dossier(SCHEMA, eid, NS, text, model_used="gpt-4o-mini")
    assert isinstance(row_id, int)

    result = store.get_entity_dossier(SCHEMA, NS, "Dossier_Entity")
    assert result is not None
    assert result["dossier_text"] == text
    assert result["name"] == "Dossier_Entity"
    assert result["model_used"] == "gpt-4o-mini"


def test_dossier_upsert_replaces_prior(db):
    conn, store = db
    eid = store.upsert_entity(SCHEMA, NS, "Upsert_Entity")
    store.store_entity_dossier(SCHEMA, eid, NS, "original text", model_used="v1")
    store.store_entity_dossier(SCHEMA, eid, NS, "updated text", model_used="v2")
    result = store.get_entity_dossier(SCHEMA, NS, "Upsert_Entity")
    assert result["dossier_text"] == "updated text"
    assert result["model_used"] == "v2"


def test_get_entity_dossier_not_found(db):
    conn, store = db
    result = store.get_entity_dossier(SCHEMA, NS, "NonExistent_xyz123")
    assert result is None


def test_get_entity_dossier_case_insensitive(db):
    conn, store = db
    eid = store.upsert_entity(SCHEMA, NS, "CaseTest_Entity")
    store.store_entity_dossier(SCHEMA, eid, NS, "some text", model_used=None)
    # lookup with different case
    result = store.get_entity_dossier(SCHEMA, NS, "casetest_entity")
    assert result is not None
    assert result["name"] == "CaseTest_Entity"


# ---------------------------------------------------------------------------
# Test: POST /entity/dossier API endpoint
# ---------------------------------------------------------------------------

def _call(method, path, token=None, body=None):
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _setup_auth(conn):
    pid = Control.create_principal(conn, "test-dossier-agent", "agent")
    Control.grant(conn, pid, NS, can_read=True, can_write=True)
    token = Control.mint_token(conn, pid, label="dossier-test")
    return token


@pytest.mark.skipif(
    not os.environ.get("MEMNOS_DSN"),
    reason="requires live server (set MEMNOS_URL + MEMNOS_DSN)")
def test_api_entity_dossier_404_when_missing(db):
    conn, store = db
    token = _setup_auth(conn)
    status, body = _call("POST", "/entity/dossier", token=token,
                         body={"namespace": NS, "entity": "no_such_entity_xyz"})
    assert status == 404, f"expected 404, got {status}: {body}"


@pytest.mark.skipif(
    not os.environ.get("MEMNOS_DSN"),
    reason="requires live server (set MEMNOS_URL + MEMNOS_DSN)")
def test_api_entity_dossier_returns_stored(db):
    conn, store = db
    token = _setup_auth(conn)
    eid = store.upsert_entity(SCHEMA, NS, "API_Dossier_Entity")
    dossier_text = "API_Dossier_Entity is tested via the HTTP endpoint."
    store.store_entity_dossier(SCHEMA, eid, NS, dossier_text, model_used="test-model")

    status, body = _call("POST", "/entity/dossier", token=token,
                         body={"namespace": NS, "entity": "API_Dossier_Entity"})
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body.get("dossier") == dossier_text
    assert body.get("entity") == "API_Dossier_Entity"
    assert "generated_at" in body


@pytest.mark.skipif(
    not os.environ.get("MEMNOS_DSN"),
    reason="requires live server (set MEMNOS_URL + MEMNOS_DSN)")
def test_api_entity_dossier_requires_entity_field(db):
    conn, store = db
    token = _setup_auth(conn)
    status, body = _call("POST", "/entity/dossier", token=token,
                         body={"namespace": NS})
    assert status == 400


# ---------------------------------------------------------------------------
# Test: MCP tool get_entity_dossier
# ---------------------------------------------------------------------------

def test_mcp_get_entity_dossier_no_server():
    """MCP tool returns a clean 'no dossier' message when no server is available,
    not a traceback."""
    import importlib
    import unittest.mock as mock

    # Patch httpx.post to simulate 404 from server
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "no dossier found for this entity"}
    mock_resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
        "404", request=mock.MagicMock(), response=mock_resp)

    with mock.patch("httpx.post", return_value=mock_resp):
        import memnos_mcp
        result = memnos_mcp.get_entity_dossier("UnknownEntity")
    assert "no dossier" in result.lower()


def test_mcp_get_entity_dossier_returns_text(db):
    """MCP tool formats dossier text correctly when the server returns it."""
    import unittest.mock as mock

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "entity": "TestEntity",
        "dossier": "TestEntity is a fictional entity used for testing.",
        "generated_at": "2026-06-28T00:00:00+00:00",
        "model_used": "gpt-4o-mini",
    }

    with mock.patch("httpx.post", return_value=mock_resp):
        import memnos_mcp
        result = memnos_mcp.get_entity_dossier("TestEntity")
    assert "TestEntity" in result
    assert "testing" in result.lower()
    assert "2026-06-28" in result


if __name__ == "__main__":
    import subprocess
    import sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
