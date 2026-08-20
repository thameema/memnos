"""memnos-sdk tests.

Unit tests use an httpx MockTransport (no server needed) — deterministic. A live smoke
test runs only if MEMNOS_TOKEN + MEMNOS_NS env are set and a server is reachable.
Run: python -m pytest sdk/tests -q     (or: python sdk/tests/test_client.py)
"""
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from memnos_sdk import AsyncMemnosClient, MemnosClient, MemnosError

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def _handler(request):
    import json
    body = json.loads(request.content or b"{}")
    if request.url.path == "/remember":
        assert body["namespace"] and body["text"]
        if body.get("async"):                    # async path: server returns immediately
            return httpx.Response(200, json={"turn_id": 1, "facts": None, "extraction": "queued"})
        return httpx.Response(200, json={"turn_id": 1, "facts": 2, "superseded": 0})
    if request.url.path == "/recall":
        return httpx.Response(200, json={"memories": [{"content": "we chose postgres", "kind": "fact",
                                                       "score": 0.9, "date": "2026-06-07"}],
                                         "context": "- (fact) we chose postgres"})
    if request.url.path == "/consolidate":
        return httpx.Response(200, json={"dossiers": 3})
    if request.url.path == "/feedback":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/secret/resolve":
        assert body.get("name")
        if body["name"] == "openai_api_key":
            return httpx.Response(200, json={"name": "openai_api_key", "value": "sk-mock-12345"})
        return httpx.Response(404, json={"error": "secret not found"})
    return httpx.Response(404, json={"error": "not found"})


def main():
    print("=== memnos-sdk (mock transport) ===")
    mem = MemnosClient(token="mnk_test", namespace="org:acme", transport=httpx.MockTransport(_handler))
    check("remember returns ids", mem.remember("we chose postgres")["facts"] == 2)
    check("remember async_ forwards async:true (returns queued)",
          mem.remember("slow-extraction turn", async_=True).get("extraction") == "queued")
    check("recall returns memories", mem.recall("db?")["memories"][0]["content"] == "we chose postgres")
    check("context returns string", mem.context("db?").startswith("- (fact)"))
    check("consolidate", mem.consolidate()["dossiers"] == 3)
    check("feedback", mem.feedback("db?", True)["ok"] is True)
    check("healthy()", mem.healthy() is True)
    check("resolve_secret returns plaintext value", mem.resolve_secret("openai_api_key") == "sk-mock-12345")
    try:
        mem.resolve_secret("no_such_secret")
        check("resolve_secret 404 -> MemnosError", False)
    except MemnosError as e:
        check("resolve_secret 404 -> MemnosError", e.status == 404)
    # namespace required if neither client-default nor per-call
    try:
        MemnosClient(token="x", transport=httpx.MockTransport(_handler)).remember("y")
        check("missing namespace raises", False)
    except ValueError:
        check("missing namespace raises", True)
    # resolve_secret is deliberately NOT namespace-scoped (issue #114): a client with no
    # namespace set at all must still be able to call it.
    nsless = MemnosClient(token="x", transport=httpx.MockTransport(_handler))
    check("resolve_secret works with no namespace set",
          nsless.resolve_secret("openai_api_key") == "sk-mock-12345")
    nsless.close()
    # error mapping
    def err_handler(req):
        return httpx.Response(403, json={"error": "forbidden for namespace"})
    em = MemnosClient(token="x", namespace="n", transport=httpx.MockTransport(err_handler))
    try:
        em.recall("q"); check("4xx -> MemnosError", False)
    except MemnosError as e:
        check("4xx -> MemnosError", e.status == 403)
    try:
        em.resolve_secret("x"); check("resolve_secret 403 -> MemnosError", False)
    except MemnosError as e:
        check("resolve_secret 403 -> MemnosError", e.status == 403)
    mem.close(); em.close()

    # async
    import asyncio
    async def _a():
        async with AsyncMemnosClient(token="x", namespace="n", transport=httpx.MockTransport(_handler)) as am:
            r = await am.recall("db?")
            return r["memories"][0]["content"]
    check("async recall", asyncio.run(_a()) == "we chose postgres")

    async def _a_secret():
        async with AsyncMemnosClient(token="x", transport=httpx.MockTransport(_handler)) as am:
            return await am.resolve_secret("openai_api_key")
    check("async resolve_secret", asyncio.run(_a_secret()) == "sk-mock-12345")

    # adapters import (skip if frameworks absent)
    try:
        from memnos_sdk.integrations.langchain import MemnosRetriever  # noqa
        check("langchain adapter imports", True)
    except ImportError:
        print("  SKIP langchain adapter (langchain_core not installed)")
    try:
        from memnos_sdk.integrations.langgraph import MemnosStore  # noqa
        check("langgraph adapter imports", True)
    except ImportError:
        print("  SKIP langgraph adapter (langgraph not installed)")

    # slow extraction must NOT block the write: on the SYNC path the server holds the request
    # through extraction (slow local-LLM); on the async_=True path it returns immediately.
    # The handler asserts the contract by sleeping ONLY when async is not set — so the async_
    # call returns fast (proving no ReadTimeout risk) while the sync call is measurably slower.
    import json
    import time as _time
    def slow_handler(req):
        b = json.loads(req.content or b"{}")
        if not b.get("async"):
            _time.sleep(0.5)                     # simulate slow synchronous extraction
        return httpx.Response(200, json={"turn_id": 1, "facts": None, "extraction": "queued"})
    sm = MemnosClient(token="x", namespace="n", transport=httpx.MockTransport(slow_handler))
    t0 = _time.perf_counter()
    out = sm.remember("async on slow backend", async_=True)
    async_dt = _time.perf_counter() - t0
    check("async_ remember returns immediately on slow backend (no extraction block)",
          out.get("extraction") == "queued" and async_dt < 0.2)
    t1 = _time.perf_counter()
    sm.remember("sync on slow backend")          # sync path waits on extraction
    sync_dt = _time.perf_counter() - t1
    check("sync remember blocks on slow extraction (async_ is the fix)", sync_dt >= 0.5)
    sm.close()

    # __version__ is read from package metadata (not the old hardcoded "0.1.0" that drifted)
    import memnos_sdk
    check("__version__ is package-derived (not stale 0.1.0)",
          memnos_sdk.__version__ != "0.1.0")

    # live smoke (optional)
    tok, ns = os.environ.get("MEMNOS_TOKEN"), os.environ.get("MEMNOS_NS")
    if tok and ns:
        live = MemnosClient(token=tok, namespace=ns)
        if live.healthy():
            live.remember("sdk live smoke: postgres chosen")
            check("LIVE recall", "context" in live.recall("what was chosen?"))
        live.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
