"""
Feature 6 — Pluggable vector backend (Qdrant as enterprise default).

Contract under test:

  * GET /admin/vector-backend reports the active backend, configured env,
    reachability, vector dim, and (when qdrant) collection + URL.
  * The factory in memnos.storage.vector_backend routes:
      - MEMNOS_VECTOR_BACKEND="qdrant"  → QdrantVectorBackend
      - MEMNOS_VECTOR_BACKEND="" / "arcadedb" → None  (built-in ArcadeDB)
      - Unknown values: log warning, return None
      - Unknown values + STRICT=true: raise RuntimeError
  * End-to-end smoke (skipped unless the dev stack is in Qdrant-primary
    mode): write a memory and confirm search via Qdrant returns it.

We don't reconfigure the running dev stack in pytest — that's a deploy
concern. We do unit-test the factory directly and we expose the
end-to-end test so the operator can opt in by:

    MEMNOS_VECTOR_BACKEND=qdrant MEMNOS_QDRANT_URL=http://localhost:16333 \\
      /tmp/memnos-dev-data/dev.sh up -d memnos
"""
from __future__ import annotations

import os
import time
import uuid
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Admin endpoint shape — always runs
# ---------------------------------------------------------------------------

class TestVectorBackendStatusEndpoint:
    def test_endpoint_returns_required_fields(self, http):
        r = http.get("/api/v1/admin/vector-backend")
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        for key in ("backend", "configured", "reachable", "vector_dim"):
            assert key in body, f"missing {key}: {body}"
        assert body["backend"] in ("arcadedb", "qdrant"), body["backend"]
        assert isinstance(body["reachable"], bool)
        assert isinstance(body["vector_dim"], int)
        assert body["vector_dim"] > 0, "vector_dim must be positive"

    def test_backend_reports_arcadedb_or_qdrant_consistently(self, http):
        """When ``backend == "arcadedb"`` the collection/url fields must be
        null. When ``backend == "qdrant"`` they must be populated. Catches
        misreporting bugs in the admin endpoint regardless of mode."""
        body = http.get("/api/v1/admin/vector-backend").json()
        if body["backend"] == "arcadedb":
            assert body.get("collection") is None
            assert body.get("url") is None
        else:
            assert body.get("collection"), f"qdrant mode missing collection: {body}"
            assert body.get("url"), f"qdrant mode missing url: {body}"


# ---------------------------------------------------------------------------
# Factory routing — unit tests, no HTTP needed
# ---------------------------------------------------------------------------

class TestFactoryRouting:
    """Direct factory unit tests — only runnable in an env where the
    ``memnos`` package is importable (inside the container, or after a
    ``pip install -e packages/core`` on the host). Skipped otherwise."""

    def _import_factory(self):
        try:
            from memnos.storage.vector_backend import create_vector_backend
        except ImportError:
            pytest.skip("memnos package not importable in this pytest env")
        return create_vector_backend

    def test_unset_env_returns_none(self):
        factory = self._import_factory()
        with patch.dict(os.environ, {"MEMNOS_VECTOR_BACKEND": "",
                                     "MEMNOS_VECTOR_BACKEND_STRICT": ""}):
            assert factory(vector_dim=768) is None

    def test_arcadedb_env_returns_none(self):
        factory = self._import_factory()
        with patch.dict(os.environ, {"MEMNOS_VECTOR_BACKEND": "arcadedb",
                                     "MEMNOS_VECTOR_BACKEND_STRICT": ""}):
            assert factory(vector_dim=768) is None

    def test_unknown_env_returns_none_when_not_strict(self):
        factory = self._import_factory()
        with patch.dict(os.environ, {"MEMNOS_VECTOR_BACKEND": "magic-store",
                                     "MEMNOS_VECTOR_BACKEND_STRICT": ""}):
            assert factory(vector_dim=768) is None

    def test_unknown_env_raises_when_strict(self):
        factory = self._import_factory()
        with patch.dict(os.environ, {"MEMNOS_VECTOR_BACKEND": "magic-store",
                                     "MEMNOS_VECTOR_BACKEND_STRICT": "true"}):
            with pytest.raises(RuntimeError):
                factory(vector_dim=768)

    def test_qdrant_env_returns_qdrant_backend(self):
        factory = self._import_factory()
        from memnos.storage.qdrant_backend import QdrantVectorBackend
        # Set both QDRANT_URL (higher precedence) and MEMNOS_QDRANT_URL so we
        # bypass any pre-existing container env (e.g. docker-compose's
        # QDRANT_URL=http://qdrant:6333).
        with patch.dict(os.environ, {
            "MEMNOS_VECTOR_BACKEND": "qdrant",
            "QDRANT_URL": "http://example:6333",
            "MEMNOS_QDRANT_URL": "http://example:6333",
            "MEMNOS_VECTOR_BACKEND_STRICT": "",
        }):
            backend = factory(vector_dim=768)
            assert isinstance(backend, QdrantVectorBackend)
            assert backend.url == "http://example:6333"

    def test_strict_qdrant_fails_when_unreachable(self):
        """STRICT + qdrant + unreachable host = RuntimeError at construction."""
        factory = self._import_factory()
        with patch.dict(os.environ, {
            "MEMNOS_VECTOR_BACKEND": "qdrant",
            "QDRANT_URL": "http://10.255.255.1:65535",
            "MEMNOS_QDRANT_URL": "http://10.255.255.1:65535",
            "MEMNOS_VECTOR_BACKEND_STRICT": "true",
        }):
            with pytest.raises(RuntimeError, match="unreachable|probe failed"):
                factory(vector_dim=768)


# ---------------------------------------------------------------------------
# End-to-end smoke — only runs when dev stack is in qdrant-primary mode
# ---------------------------------------------------------------------------

def _qdrant_primary(http) -> bool:
    try:
        body = http.get("/api/v1/admin/vector-backend").json()
        return body.get("backend") == "qdrant" and body.get("reachable") is True
    except Exception:
        return False


class TestQdrantPrimarySmoke:
    """End-to-end roundtrip when MEMNOS_VECTOR_BACKEND=qdrant is configured.

    Skipped by default. To run: set MEMNOS_VECTOR_BACKEND=qdrant in the dev
    stack's .env, restart memnos, then re-run the suite.
    """

    def test_write_then_vector_search_returns_seed(self, http):
        if not _qdrant_primary(http):
            pytest.skip("dev stack is not in qdrant-primary mode")
        ns = f"f6-qdrant-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        seed_content = f"qdrant primary smoke test seed token {uuid.uuid4().hex[:8]}"
        r = http.post(
            "/api/v1/memory/",
            json={"content": seed_content, "namespace": ns, "memory_type": "fact"},
        )
        assert r.status_code == 201
        seed_id = r.json()["id"]

        # Vector search MUST surface the seed
        results = http.get(
            "/api/v1/memory/search",
            params={"q": seed_content, "ns": ns, "mode": "vector", "top_k": 3},
        ).json()
        assert any(x["id"] == seed_id for x in results), (
            f"Qdrant-primary vector search failed to retrieve seed: {results}"
        )
