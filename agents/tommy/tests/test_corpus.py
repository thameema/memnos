"""
Unit tests for tommy/corpus.py — issue #109's direct HTTP client for
memnos's /corpus/check, /corpus/ingest, /corpus/list.

Uses httpx.MockTransport (no live server needed), the same pattern
sdk/tests/test_client.py uses for MemnosClient — this module deliberately
mirrors that client's shape (an httpx.Client built with an injectable
`transport`) for exactly this reason.

Covers the "never raises, always returns a distinguishable ok/not-ok dict"
contract every caller (mcp_server.py's corpus gate, cli.py's auto-ingest)
depends on: a 200 with a body, a 200 with an empty result, a 4xx from the
server, and a transport-level failure (unreachable).
"""
from __future__ import annotations

import httpx
import pytest

from tommy.corpus import corpus_check, corpus_ingest, corpus_list


def _handler_factory(routes: dict):
    def _handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        return route(request)
    return _handler


class TestCorpusCheck:
    def test_ok_with_matches(self):
        def check_route(request):
            return httpx.Response(200, json={"constraints": [
                {"id": 1, "content": "All writes MUST go through the ORM.", "source": "data-lld", "score": 0.9},
            ]})
        transport = httpx.MockTransport(_handler_factory({"/corpus/check": check_route}))
        result = corpus_check("http://fake", "tok", "ns:test", "raw SQL query", transport=transport)
        assert result["ok"] is True
        assert len(result["constraints"]) == 1
        assert result["constraints"][0]["source"] == "data-lld"

    def test_ok_with_no_matches_is_distinct_from_failure(self):
        transport = httpx.MockTransport(_handler_factory({
            "/corpus/check": lambda r: httpx.Response(200, json={"constraints": []}),
        }))
        result = corpus_check("http://fake", "tok", "ns:test", "irrelevant snippet", transport=transport)
        assert result["ok"] is True
        assert result["constraints"] == []
        assert "error" not in result

    def test_server_error_is_not_ok_and_never_raises(self):
        transport = httpx.MockTransport(_handler_factory({
            "/corpus/check": lambda r: httpx.Response(401, json={"error": "unauthorized"}),
        }))
        result = corpus_check("http://fake", "bad-tok", "ns:test", "snippet", transport=transport)
        assert result["ok"] is False
        assert result["constraints"] == []
        assert "unauthorized" in result["error"]

    def test_unreachable_transport_is_not_ok_and_never_raises(self):
        def _boom(request):
            raise httpx.ConnectError("connection refused", request=request)
        transport = httpx.MockTransport(_boom)
        result = corpus_check("http://fake", "tok", "ns:test", "snippet", transport=transport)
        assert result["ok"] is False
        assert result["constraints"] == []
        assert "error" in result

    def test_sends_namespace_and_snippet_and_bearer_token(self):
        captured = {}

        def check_route(request):
            import json
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"constraints": []})

        transport = httpx.MockTransport(_handler_factory({"/corpus/check": check_route}))
        corpus_check("http://fake", "mnk_abc", "org:acme", "the task text", transport=transport)
        assert captured["body"] == {"namespace": "org:acme", "snippet": "the task text"}
        assert captured["auth"] == "Bearer mnk_abc"


class TestCorpusIngest:
    def test_ok_returns_constraint_count(self):
        transport = httpx.MockTransport(_handler_factory({
            "/corpus/ingest": lambda r: httpx.Response(
                200, json={"source": "adr-1", "constraints": 3, "ids": [1, 2, 3]}),
        }))
        result = corpus_ingest("http://fake", "tok", "ns:test", "adr-1", "text", transport=transport)
        assert result == {"ok": True, "constraints": 3, "ids": [1, 2, 3]}

    def test_forbidden_for_read_only_token(self):
        transport = httpx.MockTransport(_handler_factory({
            "/corpus/ingest": lambda r: httpx.Response(403, json={"error": "forbidden for namespace"}),
        }))
        result = corpus_ingest("http://fake", "ro-tok", "ns:test", "adr-1", "text", transport=transport)
        assert result["ok"] is False
        assert "forbidden" in result["error"]

    def test_sends_name_text_kind_git_sha(self):
        captured = {}

        def ingest_route(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"constraints": 1, "ids": [1]})

        transport = httpx.MockTransport(_handler_factory({"/corpus/ingest": ingest_route}))
        corpus_ingest("http://fake", "tok", "org:acme", "docs/adr/0001.md", "Systems SHALL log.",
                      kind="doc", git_sha="deadbeef", transport=transport)
        assert captured["body"] == {
            "namespace": "org:acme", "name": "docs/adr/0001.md",
            "text": "Systems SHALL log.", "kind": "doc", "git_sha": "deadbeef",
        }

    def test_unreachable_never_raises(self):
        def _boom(request):
            raise httpx.ConnectTimeout("timed out", request=request)
        transport = httpx.MockTransport(_boom)
        result = corpus_ingest("http://fake", "tok", "ns:test", "adr-1", "text", transport=transport)
        assert result["ok"] is False
        assert "error" in result


class TestCorpusList:
    def test_ok_returns_sources(self):
        transport = httpx.MockTransport(_handler_factory({
            "/corpus/list": lambda r: httpx.Response(200, json={"sources": [
                {"name": "adr-1", "constraint_count": 3},
            ]}),
        }))
        result = corpus_list("http://fake", "tok", "ns:test", transport=transport)
        assert result["ok"] is True
        assert result["sources"][0]["constraint_count"] == 3

    def test_failure_returns_empty_sources_not_ok(self):
        def _boom(request):
            raise httpx.ConnectError("nope", request=request)
        transport = httpx.MockTransport(_boom)
        result = corpus_list("http://fake", "tok", "ns:test", transport=transport)
        assert result["ok"] is False
        assert result["sources"] == []
