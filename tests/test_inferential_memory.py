"""Tests for issue #24: inferential memory — LLM-derived conclusions from patterns
across stated facts, produced by an opt-in inference pass inside consolidate().

Tests:
  - infer_conclusions() with a mocked LLM response (happy path + every edge case)
  - BrainStore.insert_semantic() round-trips inference_confidence/basis/source_fact_ids
  - BrainStore.supersede_inferred() closes out prior live inferred rows for a subject
  - BrainStore.search_semantic() returns inference_confidence/basis in result rows
  - MemnosMemory.consolidate(infer=True) end-to-end (mocked LLM, real DB): writes an
    inferred row, and a SECOND consolidate(infer=True) call supersedes the first
  - MemnosMemory.render_context() marks an inferred row with 'confidence=<level>'
  - Integration-skip test for the /consolidate HTTP hook (requires a live server
    running with MEMNOS_INFER_ON_SLEEP=1 -- skipped otherwise)

Run:
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos \
    MEMNOS_URL=http://127.0.0.1:8900 \
    python -m pytest tests/test_inferential_memory.py -v

No LLM spend: infer_conclusions and consolidate(infer=True) are exercised with a
mocked LLM object; the /consolidate hook test is skipped unless a live server with
MEMNOS_INFER_ON_SLEEP=1 is explicitly provided.
"""
import hashlib
import json
import os
import sys
import types
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.control import Control
from core.service import MemnosMemory, infer_conclusions
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:inferential_memory"
SCHEMA = "tenant_memnos"


# ---------------------------------------------------------------------------
# Deterministic fake embedder (no network) -- dimension discovered from schema
# ---------------------------------------------------------------------------

def fake_embed(text, _dim=[None]):
    h = hashlib.sha256((text or "").encode()).digest()
    dim = _dim[0]
    v = [0.0] * dim
    for i in range(64):
        v[(h[i % 32] * 7 + i) % dim] = ((h[(i * 3) % 32] / 255.0) - 0.5)
    return v


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
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "semantic", "entities"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")   # additive/rolling-safe: lands the new columns too
    _cleanup(conn, store)
    with conn.cursor() as c:
        c.execute(f"SELECT atttypmod FROM pg_attribute WHERE attrelid='{SCHEMA}.raw_turns'::regclass "
                  f"AND attname='embedding'")
        dim = c.fetchone()["atttypmod"]
    fake_embed.__defaults__[0][0] = dim
    yield conn, store, dim
    _cleanup(conn, store)
    conn.close()


# ---------------------------------------------------------------------------
# Fake LLM stubs
# ---------------------------------------------------------------------------

class _FakeChoice:
    def __init__(self, text):
        self.message = types.SimpleNamespace(content=text)


class _FakeInferLLM:
    """Minimal stub satisfying llm.chat.completions.create() for infer_conclusions()."""
    def __init__(self, payload):
        self._text = json.dumps(payload)
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return types.SimpleNamespace(choices=[_FakeChoice(self._text)])


class _DualFakeLLM:
    """Stub for the full consolidate(infer=True) path: consolidate() calls
    self.llm.chat.completions.create() TWICE per entity -- once for the dossier
    ('facts') prompt, once for the inference ('conclusions') prompt via
    infer_conclusions(). One canned JSON payload with BOTH keys covers both call
    sites since each only reads the key it cares about (.get(key, []))."""
    def __init__(self, conclusions):
        self._text = json.dumps({"facts": [], "conclusions": conclusions})
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return types.SimpleNamespace(choices=[_FakeChoice(self._text)])


class _BrokenLLM:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("pretend network error")


# ---------------------------------------------------------------------------
# infer_conclusions() -- unit tests, no DB
# ---------------------------------------------------------------------------

def test_infer_conclusions_basic():
    facts = [(101, "ordered salad"), (102, "ordered salad again"), (103, "ordered grilled chicken")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "likely prefers lighter meals", "confidence": "medium",
         "basis": "ordered salad twice vs chicken once", "supporting_indices": [1, 2]}]})
    result = infer_conclusions("Subject", facts, llm, "test-model")
    assert len(result) == 1
    r = result[0]
    assert r["conclusion"] == "likely prefers lighter meals"
    assert r["confidence"] == "medium"
    assert r["basis"] == "ordered salad twice vs chicken once"
    assert r["supporting_fact_ids"] == [101, 102]


def test_infer_conclusions_empty_facts_returns_empty():
    assert infer_conclusions("Subject", [], _FakeInferLLM({"conclusions": []}), "test-model") == []


def test_infer_conclusions_none_llm_returns_empty():
    facts = [(1, "a"), (2, "b"), (3, "c")]
    assert infer_conclusions("Subject", facts, None, "test-model") == []


def test_infer_conclusions_fewer_than_3_facts_returns_empty():
    facts = [(1, "a"), (2, "b")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "x", "confidence": "high", "supporting_indices": [1, 2]}]})
    assert infer_conclusions("Subject", facts, llm, "test-model") == []


def test_infer_conclusions_llm_exception_returns_empty():
    facts = [(1, "a"), (2, "b"), (3, "c")]
    assert infer_conclusions("Subject", facts, _BrokenLLM(), "test-model") == []


def test_infer_conclusions_drops_low_support_conclusion():
    """A conclusion whose supporting_indices resolve to fewer than 2 valid fact ids is
    not actually a cross-fact pattern and must be dropped."""
    facts = [(1, "a"), (2, "b"), (3, "c")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "not enough support", "confidence": "low", "basis": "x",
         "supporting_indices": [1]},
        {"conclusion": "enough support", "confidence": "high", "basis": "y",
         "supporting_indices": [2, 3]},
    ]})
    result = infer_conclusions("Subject", facts, llm, "test-model")
    assert len(result) == 1
    assert result[0]["conclusion"] == "enough support"


def test_infer_conclusions_ignores_out_of_range_and_noninteger_indices():
    """Out-of-range or non-int indices are ignored, not a crash; the conclusion still
    keeps its remaining valid indices."""
    facts = [(1, "a"), (2, "b"), (3, "c")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "x", "confidence": "medium", "basis": "y",
         "supporting_indices": [1, 2, 99, "not-an-int", None]},
    ]})
    result = infer_conclusions("Subject", facts, llm, "test-model")
    assert len(result) == 1
    assert result[0]["supporting_fact_ids"] == [1, 2]


def test_infer_conclusions_invalid_confidence_defaults_to_medium():
    facts = [(1, "a"), (2, "b"), (3, "c")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "x", "confidence": "super-high", "basis": "y",
         "supporting_indices": [1, 2]}]})
    result = infer_conclusions("Subject", facts, llm, "test-model")
    assert result[0]["confidence"] == "medium"


def test_infer_conclusions_empty_conclusion_text_skipped():
    facts = [(1, "a"), (2, "b"), (3, "c")]
    llm = _FakeInferLLM({"conclusions": [
        {"conclusion": "   ", "confidence": "medium", "basis": "y", "supporting_indices": [1, 2]}]})
    assert infer_conclusions("Subject", facts, llm, "test-model") == []


def test_infer_conclusions_malformed_json_returns_empty():
    class _BadJSONLLM:
        chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=lambda **kw: types.SimpleNamespace(choices=[_FakeChoice("not json")])))
    facts = [(1, "a"), (2, "b"), (3, "c")]
    assert infer_conclusions("Subject", facts, _BadJSONLLM(), "test-model") == []


# ---------------------------------------------------------------------------
# person/agent scoping (pilot follow-up): consolidate()'s clustering groups facts
# under ANY entity mentioned 3+ times, including tools/products/topics ("Fitbit
# Inspire HR", "Curves panel") that show up as subject_entity as often as a real
# person does. A LongMemEval pilot showed these produce coherent-sounding but
# useless trivia ("The Curves panel is essential for precise color adjustments")
# instead of genuine preferences. The fix gates in the PROMPT (no hardcoded
# identity string, so it works for "user", "Alice", or any named person) rather
# than pre-filtering by caller, since only the LLM can judge person-hood from the
# fact content itself.
# ---------------------------------------------------------------------------

class _CapturingFakeLLM:
    """Like _FakeInferLLM but records the exact kwargs passed to create(), so a
    test can assert on the system prompt actually sent (regression guard against
    a future edit silently dropping the person/agent gate)."""
    def __init__(self, payload):
        self._text = json.dumps(payload)
        self.calls = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(choices=[_FakeChoice(self._text)])


def test_infer_conclusions_prompt_instructs_person_agent_gate():
    facts = [(1, "a"), (2, "b"), (3, "c")]
    llm = _CapturingFakeLLM({"conclusions": []})
    infer_conclusions("Curves panel", facts, llm, "test-model")
    assert len(llm.calls) == 1
    system_msg = llm.calls[0]["messages"][0]["content"]
    assert "PERSON or AGENT" in system_msg
    assert '"conclusions":[]' in system_msg


def test_infer_conclusions_llm_declines_nonperson_subject_returns_empty():
    """Simulates the LLM correctly recognizing a tool/product subject (per the
    prompt's person/agent gate) and declining to derive a conclusion for it."""
    facts = [(1, "The Curves panel adjusts color precisely."),
             (2, "The Curves panel supports RGB and luma curves."),
             (3, "The Curves panel is found in the color grading tab.")]
    llm = _FakeInferLLM({"conclusions": []})
    assert infer_conclusions("Curves panel", facts, llm, "test-model") == []


# ---------------------------------------------------------------------------
# BrainStore.insert_semantic -- new columns round-trip (direct DB)
# ---------------------------------------------------------------------------

def test_insert_semantic_inference_columns_roundtrip(db):
    conn, store, dim = db
    fid = store.insert_semantic(
        SCHEMA, NS, "inferred", "roundtrip test conclusion.",
        subject="roundtrip_subject", inference_confidence="high",
        inference_basis="basis text for roundtrip", source_fact_ids=[1001, 1002, 1003],
        memory_type="inferred")
    with conn.cursor() as c:
        c.execute(f"SELECT inference_confidence, inference_basis, source_fact_ids, memory_type, kind "
                  f"FROM {SCHEMA}.semantic WHERE id=%s", (fid,))
        row = c.fetchone()
    assert row["inference_confidence"] == "high"
    assert row["inference_basis"] == "basis text for roundtrip"
    assert sorted(row["source_fact_ids"]) == [1001, 1002, 1003]
    assert row["memory_type"] == "inferred"
    assert row["kind"] == "inferred"


def test_insert_semantic_without_inference_kwargs_defaults_null(db):
    """Plain fact rows (no inference kwargs passed) must NOT gain values in the new
    columns -- backward compatible with every existing insert_semantic() call site."""
    conn, store, dim = db
    fid = store.insert_semantic(SCHEMA, NS, "fact", "a plain stated fact.",
                                subject="plain_subject")
    with conn.cursor() as c:
        c.execute(f"SELECT inference_confidence, inference_basis, source_fact_ids "
                  f"FROM {SCHEMA}.semantic WHERE id=%s", (fid,))
        row = c.fetchone()
    assert row["inference_confidence"] is None
    assert row["inference_basis"] is None
    assert row["source_fact_ids"] is None


# ---------------------------------------------------------------------------
# BrainStore.supersede_inferred (direct DB)
# ---------------------------------------------------------------------------

def test_supersede_inferred(db):
    conn, store, dim = db
    subj = "supersede_subject"
    id1 = store.insert_semantic(SCHEMA, NS, "inferred", "first conclusion.", subject=subj,
                                valid_from="2026-01-01T00:00:00+00:00", memory_type="inferred",
                                inference_confidence="low", inference_basis="b1")
    id2 = store.insert_semantic(SCHEMA, NS, "inferred", "second conclusion.", subject=subj,
                                valid_from="2026-01-01T00:00:00+00:00", memory_type="inferred",
                                inference_confidence="medium", inference_basis="b2")

    n = store.supersede_inferred(SCHEMA, NS, subj, "2026-02-01T00:00:00+00:00")
    assert n == 2

    with conn.cursor() as c:
        c.execute(f"SELECT id, valid_to FROM {SCHEMA}.semantic WHERE id = ANY(%s) ORDER BY id",
                  ([id1, id2],))
        rows = c.fetchall()
    assert len(rows) == 2
    assert all(r["valid_to"] is not None for r in rows)

    id3 = store.insert_semantic(SCHEMA, NS, "inferred", "third conclusion.", subject=subj,
                                valid_from="2026-02-01T00:00:00+00:00", memory_type="inferred",
                                inference_confidence="high", inference_basis="b3")
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.semantic WHERE namespace=%s AND subject_entity=%s "
                  f"AND memory_type='inferred' AND valid_to IS NULL", (NS, subj))
        live = c.fetchall()
    assert len(live) == 1 and live[0]["id"] == id3


def test_supersede_inferred_empty_subject_noop(db):
    conn, store, dim = db
    assert store.supersede_inferred(SCHEMA, NS, "", "2026-01-01T00:00:00+00:00") == 0
    assert store.supersede_inferred(SCHEMA, NS, None, "2026-01-01T00:00:00+00:00") == 0


# ---------------------------------------------------------------------------
# BrainStore.search_semantic -- returns inference fields (direct DB, no LLM)
# ---------------------------------------------------------------------------

def test_search_semantic_returns_inference_fields(db):
    conn, store, dim = db
    text = "search_semantic_probe likes searchable inferred content zzqprobe."
    vec = fake_embed(text)
    fid = store.insert_semantic(SCHEMA, NS, "inferred", text, subject="search_semantic_probe",
                                vec=vec, memory_type="inferred", inference_confidence="medium",
                                inference_basis="probe basis text")
    results = store.search_semantic(SCHEMA, NS, vec, "zzqprobe searchable inferred content", k=10)
    match = next((r for r in results if r["id"] == fid), None)
    assert match is not None, "inferred row not returned by search_semantic"
    assert match.get("inference_confidence") == "medium"
    assert match.get("inference_basis") == "probe basis text"


def test_search_semantic_fact_row_has_null_inference_fields(db):
    """A plain stated fact must surface NULL inference fields -- the distinction is the
    whole point of the governance requirement (never conflated with a stated fact)."""
    conn, store, dim = db
    text = "search_semantic_fact_probe is a plain stated fact about widgets qqzfact."
    vec = fake_embed(text)
    fid = store.insert_semantic(SCHEMA, NS, "fact", text, subject="search_semantic_fact_probe", vec=vec)
    results = store.search_semantic(SCHEMA, NS, vec, "qqzfact widgets", k=10)
    match = next((r for r in results if r["id"] == fid), None)
    assert match is not None
    assert match.get("inference_confidence") is None
    assert match.get("inference_basis") is None


# ---------------------------------------------------------------------------
# MemnosMemory.consolidate(infer=True) -- end-to-end with mocked LLM, real DB
# ---------------------------------------------------------------------------

def test_consolidate_infer_writes_and_supersedes(db):
    conn, store, dim = db
    subj = "alice_infer_subject"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 3 lowercase, non-proper-noun-bearing facts so entity clustering (which also
    # scrapes capitalized words via _PROPER) keys ONLY off subject_entity -- avoids a
    # spurious second cluster from an incidentally-repeated capitalized word.
    statements = [
        "alice_infer_subject ordered a salad for lunch on day one.",
        "alice_infer_subject ordered a salad again for lunch on day two.",
        "alice_infer_subject ordered grilled chicken for lunch on day three.",
    ]
    fact_ids = []
    for i, stmt in enumerate(statements):
        fid = store.insert_semantic(SCHEMA, NS, "fact", stmt, subject=subj,
                                    valid_from=t0 + timedelta(days=i))
        fact_ids.append(fid)

    conclusions = [{"conclusion": "alice_infer_subject likely prefers lighter meals.",
                   "confidence": "medium", "basis": "ordered salad twice vs chicken once",
                   "supporting_indices": [1, 2]}]
    llm = _DualFakeLLM(conclusions)
    mem = MemnosMemory(store, fake_embed, dim=dim, llm=llm, extract_model="test-model")

    result = mem.consolidate(NS, infer=True)
    assert result["inferred"] >= 1
    assert "dossiers" in result

    with conn.cursor() as c:
        c.execute(f"SELECT id, statement, memory_type, inference_confidence, inference_basis, "
                  f"source_fact_ids, valid_to, confidence FROM {SCHEMA}.semantic "
                  f"WHERE namespace=%s AND subject_entity=%s AND memory_type='inferred' ORDER BY id",
                  (NS, subj))
        rows = c.fetchall()
    assert len(rows) == 1, f"expected exactly 1 inferred row for {subj}, got {len(rows)}"
    row = rows[0]
    assert row["statement"] == "alice_infer_subject likely prefers lighter meals."
    assert row["inference_confidence"] == "medium"
    assert row["inference_basis"] == "ordered salad twice vs chicken once"
    assert row["valid_to"] is None
    assert row["confidence"] == 0.6                      # CONF_WEIGHT["medium"]
    assert set(row["source_fact_ids"]) <= set(fact_ids)
    assert len(row["source_fact_ids"]) == 2
    first_id = row["id"]

    # second consolidate(infer=True) pass: must SUPERSEDE, not accumulate
    result2 = mem.consolidate(NS, infer=True)
    assert result2["inferred"] >= 1
    with conn.cursor() as c:
        c.execute(f"SELECT id, valid_to FROM {SCHEMA}.semantic WHERE namespace=%s AND subject_entity=%s "
                  f"AND memory_type='inferred' ORDER BY id", (NS, subj))
        rows2 = c.fetchall()
    old = next(r for r in rows2 if r["id"] == first_id)
    assert old["valid_to"] is not None, "first inferred row must be closed out on re-consolidation"
    live = [r for r in rows2 if r["valid_to"] is None]
    assert len(live) == 1, f"expected exactly 1 LIVE inferred row for {subj} after re-consolidation"
    assert live[0]["id"] != first_id


def test_consolidate_infer_false_writes_nothing(db):
    """infer=False (the default) must not write any inferred rows even with an LLM
    configured -- it's opt-in per the issue's governance requirement."""
    conn, store, dim = db
    subj = "no_infer_subject"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, stmt in enumerate([
        "no_infer_subject did activity one on day one.",
        "no_infer_subject did activity two on day two.",
        "no_infer_subject did activity three on day three.",
    ]):
        store.insert_semantic(SCHEMA, NS, "fact", stmt, subject=subj, valid_from=t0 + timedelta(days=i))

    llm = _DualFakeLLM([{"conclusion": "should never be written", "confidence": "high",
                         "basis": "x", "supporting_indices": [1, 2]}])
    mem = MemnosMemory(store, fake_embed, dim=dim, llm=llm, extract_model="test-model")
    result = mem.consolidate(NS)                          # infer defaults to False
    assert result.get("inferred", 0) == 0
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.semantic WHERE namespace=%s AND subject_entity=%s "
                  f"AND memory_type='inferred'", (NS, subj))
        n = c.fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# MemnosMemory.render_context -- confidence marker (issue #24 recall surfacing)
# ---------------------------------------------------------------------------

def test_render_context_marks_inferred_with_confidence():
    row = {"content": "alice_infer_subject likely prefers lighter meals.", "kind": "fact",
           "type": "inferred", "confidence": "medium", "score": 1.0}
    ctx = MemnosMemory.render_context([row])
    assert "inferred" in ctx
    assert "confidence=medium" in ctx
    assert ctx.startswith("- (inferred, confidence=medium)")


def test_render_context_stated_fact_has_no_confidence_marker():
    """A stated (non-inferred) fact must never pick up a confidence marker, even if a
    'confidence' key happens to be present in the row."""
    row = {"content": "Bob works at Initech.", "kind": "fact", "type": "fact",
           "confidence": "medium", "score": 1.0}
    ctx = MemnosMemory.render_context([row])
    assert "confidence=" not in ctx


def test_recall_surfaces_inferred_with_confidence_marker(db):
    """End-to-end: write an inferred memory directly (no LLM), then recall it through
    the real store + context() pipeline and confirm the rendered line distinguishes it
    from a stated fact with the confidence marker."""
    conn, store, dim = db
    text = "recall_probe_subject strongly prefers vegetarian dishes based on repeated orders zzrecall."
    vec = fake_embed(text)
    store.insert_semantic(SCHEMA, NS, "inferred", text, subject="recall_probe_subject",
                          vec=vec, memory_type="inferred", inference_confidence="medium",
                          inference_basis="ordered vegetarian dishes repeatedly",
                          valid_from="2026-01-01T00:00:00+00:00")
    mem = MemnosMemory(store, fake_embed, dim=dim, llm=None)
    ctx = mem.context(NS, "zzrecall vegetarian dishes preference")
    assert "inferred" in ctx
    assert "confidence=medium" in ctx


# ---------------------------------------------------------------------------
# Integration-skip: /consolidate HTTP hook (requires live server + opt-in flag)
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


def _setup_auth(conn):
    pid = Control.create_principal(conn, "test-inference-agent", "agent")
    Control.grant(conn, pid, NS, can_read=True, can_write=True)
    token = Control.mint_token(conn, pid, label="inference-test")
    return token


@pytest.mark.skipif(
    not (os.environ.get("MEMNOS_DSN") and os.environ.get("MEMNOS_INFER_ON_SLEEP") == "1"),
    reason="requires a live server started with MEMNOS_INFER_ON_SLEEP=1 (opt-in inference pass)")
def test_api_consolidate_infer_hook(db):
    conn, store, dim = db
    token = _setup_auth(conn)
    subj = "api_infer_subject"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, stmt in enumerate([
        "api_infer_subject did task one on day one.",
        "api_infer_subject did task two on day two.",
        "api_infer_subject did task three on day three.",
    ]):
        store.insert_semantic(SCHEMA, NS, "fact", stmt, subject=subj, valid_from=t0 + timedelta(days=i))
    status, body = _call("POST", "/consolidate", token=token, body={"namespace": NS})
    assert status == 200, f"expected 200, got {status}: {body}"
    assert "dossiers" in body
    assert "inferred" in body


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
