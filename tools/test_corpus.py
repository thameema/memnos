#!/usr/bin/env python3
"""
test_corpus.py — Tests for engram's corpus ingestion and architecture enforcement feature.

Part A — Unit tests (no API, no runner fixture)
    - Severity detection regex: SHALL / MUST NOT / SHOULD / MAY / None
    - Section-aware markdown parsing
    - Node extraction (constraint / decision / fact types)
    - Connector REGISTRY lookup and error handling
    - GitDocConnector extract on a temp directory
    - CorpusStore CRUD and sync-state updates (in-memory SQLite)
    - CheckResult helpers: shall_violations, should_violations, format()
    - SDK model parsing: _parse_corpus, _parse_check

Part B — Integration tests (require a live engram API; use runner fixture)
    - POST /corpus/        — register a corpus, get 201 response
    - GET  /corpus/        — list all corpora
    - GET  /corpus/{id}    — get a specific corpus
    - GET  /corpus/bad-id  — 404 for unknown corpus
    - POST /corpus/{id}/sync  — trigger re-sync
    - POST /corpus/{id}/check — 409 when corpus not ready
    - DELETE /corpus/{id}  — 204; subsequent GET returns 404
    - SDK: corpus.register() + corpus.list() round-trip

Usage:
    python3 tools/test_corpus.py
    python3 tools/test_corpus.py --verbose
    python3 tools/test_corpus.py --test corpus_register
    python3 tools/test_corpus.py --skip-integration
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Callable

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _REPO_ROOT + "/packages/core")
sys.path.insert(0, _REPO_ROOT + "/packages/sdk")

import pytest

try:
    import httpx
except ImportError:
    print("[error] Missing package: httpx  (pip install httpx)", file=sys.stderr)
    sys.exit(1)

ENGRAM_API = os.environ.get("ENGRAM_API", "http://localhost:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_KEY", "engram-local-dev-key")

TEST_NS = f"test:corpus:{uuid.uuid4().hex[:8]}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def uid() -> str:
    return uuid.uuid4().hex[:8]


def _delete_corpus(corpus_id: str, client: httpx.Client) -> None:
    try:
        client.delete(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test runner (same pattern as test_subscriptions.py)
# ---------------------------------------------------------------------------

@dataclass
class Runner:
    verbose: bool = False
    only: str | None = None
    skip_integration: bool = False
    results: list[tuple[str, bool, str, float]] = field(default_factory=list)

    def run(self, fn: Callable[["Runner"], None]) -> None:
        name = fn.__name__.removeprefix("test_")
        if self.only and self.only != name:
            return
        t0 = time.monotonic()
        try:
            fn(self)
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, True, "", elapsed))
            print(f"  \033[32m✓\033[0m {name}  ({elapsed:.0f}ms)")
        except pytest.skip.Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, True, f"skipped: {e}", elapsed))
            print(f"  \033[33m~\033[0m {name}  (skipped: {e})")
        except AssertionError as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, False, str(e), elapsed))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {e}")
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, False, f"{type(e).__name__}: {e}", elapsed))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {type(e).__name__}: {e}")

    def summarise(self) -> int:
        total = len(self.results)
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        elapsed = sum(ms for _, _, _, ms in self.results)
        print()
        print("=" * 70)
        print(f"Results: {passed}/{total} passed, {total - passed} failed  ({elapsed:.0f}ms total)")
        if passed == total:
            print("\nAll tests passed.")
        else:
            for name, ok, msg, _ in self.results:
                if not ok:
                    print(f"  \033[31m✗\033[0m {name}: {msg}")
        return 0 if passed == total else 1


# ===========================================================================
# Part A — Unit tests (no server required)
# ===========================================================================

# --- severity detection ---

def test_severity_shall() -> None:
    from engram.corpus.extractor import _severity
    assert _severity("The service SHALL validate the JWT token.") == "SHALL"
    assert _severity("Authentication MUST be enforced on all endpoints.") == "SHALL"
    assert _severity("REQUIRED field: patient ID.") == "SHALL"
    assert _severity("PROHIBITED: storing plain-text passwords.") == "SHALL"


def test_severity_must_not() -> None:
    from engram.corpus.extractor import _severity
    # MUST NOT and SHALL NOT map to SHALL severity (highest)
    assert _severity("Services MUST NOT cache OAuth tokens in plain text.") == "SHALL"
    assert _severity("The API SHALL NOT expose internal stack traces.") == "SHALL"


def test_severity_should() -> None:
    from engram.corpus.extractor import _severity
    assert _severity("All responses SHOULD include a correlation ID.") == "SHOULD"
    assert _severity("Using HTTPS is RECOMMENDED for all environments.") == "SHOULD"
    assert _severity("Direct DB access is NOT RECOMMENDED from service layers.") == "SHOULD"


def test_severity_may() -> None:
    from engram.corpus.extractor import _severity
    assert _severity("Callers MAY omit the X-Request-ID header.") == "MAY"
    assert _severity("Pagination is OPTIONAL for small result sets.") == "MAY"
    assert _severity("Clients CAN cache responses up to max-age seconds.") == "MAY"


def test_severity_none() -> None:
    from engram.corpus.extractor import _severity
    assert _severity("This service handles patient data.") is None
    assert _severity("The endpoint accepts JSON payloads.") is None
    assert _severity("") is None


# --- section parsing ---

def test_parse_sections_preamble_only() -> None:
    from engram.corpus.extractor import _parse_sections
    sections = _parse_sections("No headings here.\nJust a plain paragraph.")
    assert len(sections) == 1
    assert sections[0].heading == "__preamble__"
    assert any("No headings" in l for l in sections[0].lines)


def test_parse_sections_splits_on_headings() -> None:
    from engram.corpus.extractor import _parse_sections
    text = dedent("""\
        Preamble line.
        ## Overview
        Overview content.
        ### Security
        Security content here.
    """)
    sections = _parse_sections(text)
    headings = [s.heading for s in sections]
    assert "__preamble__" in headings
    assert "Overview" in headings
    assert "Security" in headings
    assert len(sections) == 3


def test_parse_sections_heading_level() -> None:
    from engram.corpus.extractor import _parse_sections
    sections = _parse_sections("# H1\n## H2\n### H3")
    levels = [s.level for s in sections]
    assert levels == [0, 1, 2, 3]


# --- file extraction ---

def test_extract_file_yields_constraint_for_shall() -> None:
    from engram.corpus.extractor import extract_file
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "arch.md"
        p.write_text(
            "## Security\n"
            "The service SHALL validate the access token on every request.\n"
        )
        nodes = extract_file(p, corpus_id="c1", namespace="test:ns")
    constraints = [n for n in nodes if n.memory_type == "constraint"]
    assert len(constraints) >= 1
    assert "SHALL" in constraints[0].content
    assert "corpus:c1" in constraints[0].tags
    assert "severity:SHALL" in constraints[0].tags
    assert constraints[0].severity == "SHALL"


def test_extract_file_must_not_label() -> None:
    from engram.corpus.extractor import extract_file
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "security.md"
        p.write_text(
            "## Constraints\n"
            "Services MUST NOT cache tokens in plaintext storage.\n"
        )
        nodes = extract_file(p, corpus_id="c2", namespace="test:ns")
    assert len(nodes) >= 1
    # MUST NOT label should appear in content
    assert any("MUST NOT" in n.content for n in nodes)


def test_extract_file_decision_section() -> None:
    from engram.corpus.extractor import extract_file
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "design.md"
        p.write_text(
            "## Decision\n"
            "We chose ArcadeDB over Neo4j due to native multi-model support and cost.\n"
        )
        nodes = extract_file(p, corpus_id="c3", namespace="test:ns")
    decisions = [n for n in nodes if n.memory_type == "decision"]
    assert len(decisions) >= 1
    assert "[DECISION]" in decisions[0].content


def test_extract_file_skips_short_sentences() -> None:
    from engram.corpus.extractor import extract_file
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "short.md"
        p.write_text("## Notes\nOK.\nYes.\nSHALL work.\n")
        nodes = extract_file(p, corpus_id="c4", namespace="test:ns")
    # Sentences under 20 chars are skipped ("SHALL work." is 11 chars)
    for n in nodes:
        assert len(n.content) >= 20


def test_extract_file_metadata_fields() -> None:
    from engram.corpus.extractor import extract_file
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "meta.md"
        p.write_text(
            "## API\n"
            "The endpoint SHALL return a FHIR Bundle resource with pagination links "
            "conforming to the base FHIR specification.\n"
        )
        nodes = extract_file(p, corpus_id="meta-corpus", namespace="test:ns", git_sha="abc1234")
    assert len(nodes) >= 1
    meta = nodes[0].metadata
    assert meta["corpus_id"] == "meta-corpus"
    assert meta["git_sha"] == "abc1234"
    assert "source_file" in meta
    assert "section" in meta


def test_extract_corpus_walks_directory() -> None:
    from engram.corpus.extractor import extract_corpus
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.md").write_text(
            "## Auth\nThe service SHALL use OAuth2 for all external API calls.\n"
        )
        (root / "b.md").write_text(
            "## Data\nAll PHI data MUST be encrypted at rest using AES-256 or stronger.\n"
        )
        (root / "readme.txt").write_text("ignored")
        nodes = extract_corpus(root, "**/*.md", "corpus-x", "test:ns")
    assert len(nodes) >= 2
    source_files = {n.source_file for n in nodes}
    assert any("a.md" in f for f in source_files)
    assert any("b.md" in f for f in source_files)


# --- connector registry ---

def test_connector_registry_returns_git_doc() -> None:
    from engram.corpus.connectors import REGISTRY, ConnectorType, GitDocConnector, get_connector
    assert ConnectorType.GIT_DOC in REGISTRY
    connector = get_connector(
        ConnectorType.GIT_DOC,
        corpus_id="test",
        namespace="test:ns",
        source_path="/tmp",
    )
    assert isinstance(connector, GitDocConnector)


def test_connector_registry_unknown_type_raises() -> None:
    from engram.corpus.connectors import get_connector
    try:
        get_connector("nonexistent-type", corpus_id="x", namespace="y", source_path="/z")
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "nonexistent-type" in str(e)


def test_git_doc_connector_extract_from_temp_dir() -> None:
    from engram.corpus.connectors import get_connector, ConnectorType
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "spec.md"
        p.write_text(
            "## Constraints\n"
            "The prior auth service SHALL respond within 72 hours per CMS-0057-F requirements.\n"
            "Payers SHOULD support standard X12 278 transaction sets for PA requests.\n"
        )
        connector = get_connector(
            ConnectorType.GIT_DOC,
            corpus_id="test-git-doc",
            namespace="test:ns",
            source_path=tmp,
            path_pattern="**/*.md",
        )
        nodes = asyncio.run(connector.extract())

    assert len(nodes) >= 2
    shall_nodes = [n for n in nodes if n.severity == "SHALL"]
    should_nodes = [n for n in nodes if n.severity == "SHOULD"]
    assert len(shall_nodes) >= 1
    assert len(should_nodes) >= 1


# --- CorpusStore CRUD ---

def test_corpus_store_create_and_get() -> None:
    from engram.corpus.store import CorpusStore
    from engram.models import Corpus

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = CorpusStore(db_path)
            await store.init()

            corpus = Corpus(
                name="test-corpus",
                source_path="/tmp/docs",
                namespace="test:ns",
                created_by="tester",
            )
            await store.create(corpus)

            fetched = await store.get(corpus.id)
            assert fetched is not None
            assert fetched.id == corpus.id
            assert fetched.name == "test-corpus"
            assert fetched.namespace == "test:ns"
            assert fetched.status == "pending"
        finally:
            Path(db_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_corpus_store_list_all() -> None:
    from engram.corpus.store import CorpusStore
    from engram.models import Corpus

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = CorpusStore(db_path)
            await store.init()

            c1 = Corpus(name="c1", source_path="/tmp/a", namespace="ns:a", created_by="u")
            c2 = Corpus(name="c2", source_path="/tmp/b", namespace="ns:b", created_by="u")
            await store.create(c1)
            await store.create(c2)

            all_corpora = await store.list_all()
            ids = {c.id for c in all_corpora}
            assert c1.id in ids
            assert c2.id in ids
        finally:
            Path(db_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_corpus_store_delete() -> None:
    from engram.corpus.store import CorpusStore
    from engram.models import Corpus

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = CorpusStore(db_path)
            await store.init()

            corpus = Corpus(name="to-delete", source_path="/tmp/x", namespace="ns:x", created_by="u")
            await store.create(corpus)

            deleted = await store.delete(corpus.id)
            assert deleted is True

            fetched = await store.get(corpus.id)
            assert fetched is None

            second_delete = await store.delete(corpus.id)
            assert second_delete is False
        finally:
            Path(db_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_corpus_store_update_sync_state() -> None:
    from engram.corpus.store import CorpusStore
    from engram.models import Corpus

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = CorpusStore(db_path)
            await store.init()

            corpus = Corpus(name="sync-test", source_path="/tmp/y", namespace="ns:y", created_by="u")
            await store.create(corpus)

            await store.update_sync_state(
                corpus.id,
                status="ready",
                node_count=42,
                last_sync_sha="deadbeef",
            )

            updated = await store.get(corpus.id)
            assert updated.status == "ready"
            assert updated.node_count == 42
            assert updated.last_sync_sha == "deadbeef"
            assert updated.last_sync_at is not None
        finally:
            Path(db_path).unlink(missing_ok=True)

    asyncio.run(_run())


def test_corpus_store_update_sync_state_error() -> None:
    from engram.corpus.store import CorpusStore
    from engram.models import Corpus

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = CorpusStore(db_path)
            await store.init()

            corpus = Corpus(name="err-test", source_path="/bad/path", namespace="ns:z", created_by="u")
            await store.create(corpus)

            await store.update_sync_state(
                corpus.id,
                status="error",
                error_msg="git clone failed: connection refused",
            )

            updated = await store.get(corpus.id)
            assert updated.status == "error"
            assert "connection refused" in updated.error_msg
        finally:
            Path(db_path).unlink(missing_ok=True)

    asyncio.run(_run())


# --- CheckResult helpers ---

def test_check_result_shall_violations() -> None:
    from engram_sdk.models import CheckResult, ConstraintHit
    result = CheckResult(
        corpus_id="c1",
        namespace="ns",
        constraints=[
            ConstraintHit(memory_id="1", content="...", severity="SHALL", source_file="f", section="s", score=0.9),
            ConstraintHit(memory_id="2", content="...", severity="SHOULD", source_file="f", section="s", score=0.8),
            ConstraintHit(memory_id="3", content="...", severity="SHALL", source_file="f", section="s", score=0.7),
            ConstraintHit(memory_id="4", content="...", severity="MAY", source_file="f", section="s", score=0.6),
        ],
    )
    violations = result.shall_violations
    assert len(violations) == 2
    assert all(v.severity == "SHALL" for v in violations)


def test_check_result_should_violations() -> None:
    from engram_sdk.models import CheckResult, ConstraintHit
    result = CheckResult(
        corpus_id="c1",
        namespace="ns",
        constraints=[
            ConstraintHit(memory_id="1", content="...", severity="SHALL", source_file="f", section="s", score=0.9),
            ConstraintHit(memory_id="2", content="...", severity="SHOULD", source_file="f", section="s", score=0.8),
            ConstraintHit(memory_id="3", content="...", severity="MAY", source_file="f", section="s", score=0.7),
        ],
    )
    violations = result.should_violations
    assert len(violations) == 1
    assert violations[0].memory_id == "2"


def test_check_result_empty_returns_no_violations() -> None:
    from engram_sdk.models import CheckResult
    result = CheckResult(corpus_id="c1", namespace="ns", constraints=[])
    assert result.shall_violations == []
    assert result.should_violations == []


def test_check_result_format_empty() -> None:
    from engram_sdk.models import CheckResult
    result = CheckResult(corpus_id="corpus-123", namespace="ns", constraints=[])
    fmt = result.format()
    assert "corpus-123" in fmt
    assert "No constraints" in fmt


def test_check_result_format_with_constraints() -> None:
    from engram_sdk.models import CheckResult, ConstraintHit
    result = CheckResult(
        corpus_id="c1",
        namespace="test:ns",
        constraints=[
            ConstraintHit(
                memory_id="m1",
                content="[CONSTRAINT|SHALL] The service SHALL validate tokens.",
                severity="SHALL",
                source_file="arch/security.md",
                section="Security",
                score=0.92,
            ),
        ],
    )
    fmt = result.format()
    assert "[SHALL]" in fmt
    assert "validate tokens" in fmt
    assert "arch/security.md" in fmt
    assert "0.920" in fmt


# --- SDK model parsing ---

def test_sdk_parse_corpus_dict() -> None:
    from engram_sdk.corpus import _parse_corpus
    from engram_sdk.models import CorpusStatus
    data = {
        "id": "abc123",
        "name": "test-corpus",
        "source_path": "/repos/docs",
        "path_pattern": "**/*.md",
        "namespace": "org:test:arch",
        "connector_type": "git-doc",
        "watch": True,
        "status": "ready",
        "node_count": 55,
        "last_sync_sha": "cafe1234",
        "last_sync_at": None,
        "error_msg": "",
        "created_at": "2026-01-15T10:00:00+00:00",
        "created_by": "tester",
    }
    info = _parse_corpus(data)
    assert info.id == "abc123"
    assert info.name == "test-corpus"
    assert info.status == CorpusStatus.READY
    assert info.node_count == 55
    assert info.connector_type == "git-doc"
    assert info.watch is True


def test_sdk_parse_check_dict() -> None:
    from engram_sdk.corpus import _parse_check
    data = {
        "corpus_id": "c1",
        "namespace": "org:test",
        "constraints": [
            {
                "memory_id": "m1",
                "content": "[CONSTRAINT|SHALL] Services SHALL use mTLS.",
                "severity": "SHALL",
                "source_file": "security.md",
                "section": "Transport Security",
                "score": 0.95,
            },
            {
                "memory_id": "m2",
                "content": "[CONSTRAINT|SHOULD] Logs SHOULD include trace IDs.",
                "severity": "SHOULD",
                "source_file": "observability.md",
                "section": "Logging",
                "score": 0.78,
            },
        ],
    }
    result = _parse_check(data)
    assert result.corpus_id == "c1"
    assert len(result.constraints) == 2
    assert result.constraints[0].severity == "SHALL"
    assert result.constraints[1].severity == "SHOULD"
    assert result.shall_violations[0].memory_id == "m1"
    assert result.should_violations[0].memory_id == "m2"


def test_sdk_parse_corpus_missing_optional_fields() -> None:
    from engram_sdk.corpus import _parse_corpus
    from engram_sdk.models import CorpusStatus
    data = {
        "id": "min-id",
        "name": "minimal",
        "source_path": "/x",
        "namespace": "ns",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    info = _parse_corpus(data)
    assert info.status == CorpusStatus.PENDING
    assert info.node_count == 0
    assert info.error_msg == ""
    assert info.watch is False


# ===========================================================================
# Part B — Integration tests (require live API; use runner fixture)
# ===========================================================================

def test_corpus_register(runner: Runner) -> None:
    """POST /corpus/ creates a corpus record and returns 201."""
    corpus_id: str | None = None
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        try:
            r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/",
                json={
                    "name": f"test-corpus-{uid()}",
                    "source_path": "/tmp/nonexistent-docs",
                    "namespace": TEST_NS,
                    "path_pattern": "**/*.md",
                    "watch": False,
                },
            )
            assert r.status_code == 201, (
                f"expected 201, got {r.status_code}: {r.text}"
            )
            body = r.json()
            assert "id" in body, f"response missing 'id': {body}"
            assert body["namespace"] == TEST_NS
            assert body["status"] in ("pending", "syncing", "ready", "error")
            corpus_id = body["id"]

            if runner.verbose:
                print(f"\n    corpus_id={corpus_id}")
                print(f"    status={body['status']}")
        finally:
            if corpus_id:
                _delete_corpus(corpus_id, client)


def test_corpus_list(runner: Runner) -> None:
    """GET /corpus/ returns a list that includes the registered corpus."""
    corpus_id: str | None = None
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        try:
            create_r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/",
                json={
                    "name": f"list-test-{uid()}",
                    "source_path": "/tmp/list-test-docs",
                    "namespace": TEST_NS + ":list",
                    "path_pattern": "**/*.md",
                },
            )
            assert create_r.status_code == 201
            corpus_id = create_r.json()["id"]

            list_r = client.get(f"{ENGRAM_API}/api/v1/corpus/")
            assert list_r.status_code == 200
            items = list_r.json()
            assert isinstance(items, list), f"expected list, got {type(items)}"
            ids = [item["id"] for item in items]
            assert corpus_id in ids, (
                f"newly registered corpus {corpus_id} not in list: {ids}"
            )
        finally:
            if corpus_id:
                _delete_corpus(corpus_id, client)


def test_corpus_get(runner: Runner) -> None:
    """GET /corpus/{id} returns the correct corpus record."""
    corpus_id: str | None = None
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        try:
            name = f"get-test-{uid()}"
            create_r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/",
                json={
                    "name": name,
                    "source_path": "/tmp/get-test-docs",
                    "namespace": TEST_NS + ":get",
                    "path_pattern": "**/*.md",
                },
            )
            assert create_r.status_code == 201
            corpus_id = create_r.json()["id"]

            get_r = client.get(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}")
            assert get_r.status_code == 200
            body = get_r.json()
            assert body["id"] == corpus_id
            assert body["name"] == name

            if runner.verbose:
                print(f"\n    GET /corpus/{corpus_id} → {body['status']}")
        finally:
            if corpus_id:
                _delete_corpus(corpus_id, client)


def test_corpus_get_not_found(runner: Runner) -> None:
    """GET /corpus/nonexistent returns 404."""
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        r = client.get(f"{ENGRAM_API}/api/v1/corpus/nonexistent-id-{uid()}")
        assert r.status_code == 404, (
            f"expected 404 for unknown corpus, got {r.status_code}: {r.text}"
        )


def test_corpus_sync_trigger(runner: Runner) -> None:
    """POST /corpus/{id}/sync returns 200 and marks corpus for re-sync."""
    corpus_id: str | None = None
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        try:
            create_r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/",
                json={
                    "name": f"sync-test-{uid()}",
                    "source_path": "/tmp/sync-docs",
                    "namespace": TEST_NS + ":sync",
                },
            )
            assert create_r.status_code == 201
            corpus_id = create_r.json()["id"]

            # Wait briefly then trigger sync; server may still be in initial sync
            time.sleep(0.5)

            sync_r = client.post(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}/sync", json={})
            # 200 = sync triggered; 409 = already syncing (both acceptable)
            assert sync_r.status_code in (200, 409), (
                f"unexpected sync status: {sync_r.status_code}: {sync_r.text}"
            )
            if runner.verbose:
                print(f"\n    sync response: {sync_r.status_code}")
        finally:
            if corpus_id:
                _delete_corpus(corpus_id, client)


def test_corpus_check_not_ready_returns_409(runner: Runner) -> None:
    """POST /corpus/{id}/check on a non-ready corpus returns 409."""
    corpus_id: str | None = None
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        try:
            create_r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/",
                json={
                    "name": f"check-not-ready-{uid()}",
                    "source_path": "/tmp/definitely-does-not-exist-" + uid(),
                    "namespace": TEST_NS + ":check-nr",
                },
            )
            assert create_r.status_code == 201
            corpus_id = create_r.json()["id"]

            # Wait a moment so the background sync settles to "error" (path doesn't exist)
            time.sleep(1.5)

            body = client.get(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}").json()
            if body["status"] not in ("error", "pending"):
                pytest.skip(
                    f"corpus settled to unexpected status {body['status']!r} — "
                    "cannot test 409 guard"
                )

            check_r = client.post(
                f"{ENGRAM_API}/api/v1/corpus/{corpus_id}/check",
                json={"code": "public class Foo {}", "context": "test"},
            )
            assert check_r.status_code == 409, (
                f"expected 409 for non-ready corpus, got {check_r.status_code}: {check_r.text}"
            )

            if runner.verbose:
                print(f"\n    corpus status={body['status']}  check → {check_r.status_code}")
        finally:
            if corpus_id:
                _delete_corpus(corpus_id, client)


def test_corpus_delete(runner: Runner) -> None:
    """DELETE /corpus/{id} returns 204; subsequent GET returns 404."""
    with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=30) as client:
        create_r = client.post(
            f"{ENGRAM_API}/api/v1/corpus/",
            json={
                "name": f"delete-test-{uid()}",
                "source_path": "/tmp/delete-docs",
                "namespace": TEST_NS + ":del",
            },
        )
        assert create_r.status_code == 201
        corpus_id = create_r.json()["id"]

        del_r = client.delete(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}")
        assert del_r.status_code == 204, (
            f"expected 204, got {del_r.status_code}: {del_r.text}"
        )

        get_r = client.get(f"{ENGRAM_API}/api/v1/corpus/{corpus_id}")
        assert get_r.status_code == 404, (
            f"expected 404 after delete, got {get_r.status_code}: {get_r.text}"
        )

        if runner.verbose:
            print(f"\n    DELETE → 204  GET after delete → 404  ✓")


def test_sdk_corpus_register_and_list(runner: Runner) -> None:
    """SDK SyncCorpusClient.register() + list() round-trip."""
    try:
        from engram_sdk import EngramClient
        from engram_sdk.models import CorpusStatus
    except ImportError as e:
        pytest.skip(f"engram_sdk not installed: {e}")

    corpus_id: str | None = None
    try:
        with EngramClient(url=ENGRAM_API, api_key=ENGRAM_KEY) as client:
            name = f"sdk-corpus-{uid()}"
            info = client.corpus.register(
                name=name,
                source_path="/tmp/sdk-test-docs",
                namespace=TEST_NS + ":sdk",
                path_pattern="**/*.md",
                watch=False,
            )
            corpus_id = info.id
            assert info.name == name
            assert info.status in (CorpusStatus.PENDING, CorpusStatus.SYNCING,
                                   CorpusStatus.READY, CorpusStatus.ERROR)

            corpora = client.corpus.list()
            ids = [c.id for c in corpora]
            assert corpus_id in ids, (
                f"registered corpus {corpus_id} not in SDK list: {ids}"
            )

            if runner.verbose:
                print(f"\n    SDK corpus_id={corpus_id}  status={info.status.value}")
    finally:
        if corpus_id:
            with httpx.Client(headers={"X-API-Key": ENGRAM_KEY}, timeout=10) as client:
                _delete_corpus(corpus_id, client)


# ===========================================================================
# Entry point
# ===========================================================================

@dataclass
class UnitRunner:
    """Lightweight runner for zero-argument unit test functions."""
    verbose: bool = False
    results: list[tuple[str, bool, str, float]] = field(default_factory=list)

    def run(self, fn) -> None:
        name = fn.__name__.removeprefix("test_")
        t0 = time.monotonic()
        try:
            fn()
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, True, "", elapsed))
            print(f"  \033[32m✓\033[0m {name}  ({elapsed:.0f}ms)")
        except pytest.skip.Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, True, f"skipped: {e}", elapsed))
            print(f"  \033[33m~\033[0m {name}  (skipped: {e})")
        except AssertionError as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, False, str(e), elapsed))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {e}")
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            self.results.append((name, False, f"{type(e).__name__}: {e}", elapsed))
            print(f"  \033[31m✗\033[0m {name}  ({elapsed:.0f}ms)")
            print(f"    → {type(e).__name__}: {e}")

    def summarise(self) -> int:
        total = len(self.results)
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        elapsed = sum(ms for _, _, _, ms in self.results)
        print()
        print("=" * 70)
        print(f"Results: {passed}/{total} passed, {total - passed} failed  ({elapsed:.0f}ms total)")
        if passed == total:
            print("\nAll tests passed.")
        else:
            for name, ok, msg, _ in self.results:
                if not ok:
                    print(f"  \033[31m✗\033[0m {name}: {msg}")
        return 0 if passed == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="engram corpus tests")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--test", metavar="NAME", help="run one integration test by name")
    parser.add_argument("--skip-integration", action="store_true",
                        help="run unit tests only")
    args = parser.parse_args()

    print("engram Corpus Tests")
    print("=" * 70)
    print()

    # Part A — Unit tests (always run; these take no arguments)
    print("Part A — Unit tests")
    print("-" * 40)
    unit_tests = [
        test_severity_shall,
        test_severity_must_not,
        test_severity_should,
        test_severity_may,
        test_severity_none,
        test_parse_sections_preamble_only,
        test_parse_sections_splits_on_headings,
        test_parse_sections_heading_level,
        test_extract_file_yields_constraint_for_shall,
        test_extract_file_must_not_label,
        test_extract_file_decision_section,
        test_extract_file_skips_short_sentences,
        test_extract_file_metadata_fields,
        test_extract_corpus_walks_directory,
        test_connector_registry_returns_git_doc,
        test_connector_registry_unknown_type_raises,
        test_git_doc_connector_extract_from_temp_dir,
        test_corpus_store_create_and_get,
        test_corpus_store_list_all,
        test_corpus_store_delete,
        test_corpus_store_update_sync_state,
        test_corpus_store_update_sync_state_error,
        test_check_result_shall_violations,
        test_check_result_should_violations,
        test_check_result_empty_returns_no_violations,
        test_check_result_format_empty,
        test_check_result_format_with_constraints,
        test_sdk_parse_corpus_dict,
        test_sdk_parse_check_dict,
        test_sdk_parse_corpus_missing_optional_fields,
    ]

    unit_runner = UnitRunner(verbose=args.verbose)
    for fn in unit_tests:
        unit_runner.run(fn)

    if args.skip_integration:
        return unit_runner.summarise()

    print()
    print("Part B — Integration tests")
    print(f"API: {ENGRAM_API}   namespace: {TEST_NS}")
    print("-" * 40)

    try:
        with httpx.Client(timeout=4) as c:
            r = c.get(
                f"{ENGRAM_API}/api/v1/admin/health",
                headers={"X-API-Key": ENGRAM_KEY},
            )
            if r.status_code != 200:
                print(
                    f"[skip] engram not healthy ({r.status_code}) — "
                    "integration tests skipped (use --skip-integration to suppress this)",
                    file=sys.stderr,
                )
                return unit_runner.summarise()
    except Exception as e:
        print(
            f"[skip] Cannot reach engram at {ENGRAM_API}: {e} — "
            "integration tests skipped",
            file=sys.stderr,
        )
        return unit_runner.summarise()

    integration_tests = [
        test_corpus_register,
        test_corpus_list,
        test_corpus_get,
        test_corpus_get_not_found,
        test_corpus_sync_trigger,
        test_corpus_check_not_ready_returns_409,
        test_corpus_delete,
        test_sdk_corpus_register_and_list,
    ]

    int_runner = Runner(verbose=args.verbose, only=args.test)
    for fn in integration_tests:
        int_runner.run(fn)

    # Merge and report combined results
    all_results = unit_runner.results + int_runner.results
    total = len(all_results)
    passed = sum(1 for _, ok, _, _ in all_results if ok)
    elapsed = sum(ms for _, _, _, ms in all_results)
    print()
    print("=" * 70)
    print(f"Results: {passed}/{total} passed, {total - passed} failed  ({elapsed:.0f}ms total)")
    if passed == total:
        print("\nAll tests passed.")
    else:
        for name, ok, msg, _ in all_results:
            if not ok:
                print(f"  \033[31m✗\033[0m {name}: {msg}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
