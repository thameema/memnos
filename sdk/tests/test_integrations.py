"""Adapter tests for memnos_sdk integrations (LangChain, LangGraph, LlamaIndex), driven
by an httpx MockTransport — no live server. Each adapter section is skipped if its
framework isn't installed, so this runs standalone. Exits non-zero on any failure.

    python sdk/tests/test_integrations.py
"""
import json
import sys

import httpx

sys.path.insert(0, "sdk")
sys.path.insert(0, ".")
from memnos_sdk import MemnosClient

PASS = FAIL = SKIP = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def skip(name):
    global SKIP
    print(f"  SKIP  {name}")
    SKIP += 1


# canned server responses
REMEMBERED = []


def handler(request):
    path = request.url.path
    if path == "/recall":
        return httpx.Response(200, json={"memories": [
            {"id": 1, "content": "We chose PostgreSQL + pgvector over a graph DB", "kind": "fact", "score": 0.92, "date": "2026-01-01"},
            {"id": 2, "content": "Redis OOM on prod-02 fixed with allkeys-lru", "kind": "incident", "score": 0.71}],
            "context": "..."})
    if path == "/remember":
        REMEMBERED.append(json.loads(request.content))
        return httpx.Response(200, json={"turn_id": 99, "facts": 1, "superseded": 0})
    return httpx.Response(404, json={"error": "not found"})


def make_client():
    return MemnosClient(base_url="http://test", token="mnk_x", namespace="org:acme",
                        transport=httpx.MockTransport(handler))


def test_langchain():
    try:
        from memnos_sdk.integrations.langchain import MemnosRetriever
    except ImportError:
        return skip("langchain adapter (langchain_core not installed)")
    r = MemnosRetriever(client=make_client(), k=8)
    docs = r.invoke("what database did we choose?")
    check("langchain: returns documents", len(docs) == 2)
    check("langchain: content mapped", "PostgreSQL" in docs[0].page_content)
    check("langchain: metadata kind+score", docs[0].metadata.get("kind") == "fact" and docs[0].metadata.get("score") == 0.92)
    REMEMBERED.clear()
    r.save("We use JWT with 24h expiry")
    check("langchain: save -> remember", REMEMBERED and REMEMBERED[0]["text"] == "We use JWT with 24h expiry")


def test_llamaindex():
    try:
        from memnos_sdk.integrations.llamaindex import MemnosRetriever
    except ImportError:
        return skip("llamaindex adapter (llama-index-core not installed)")
    r = MemnosRetriever(client=make_client(), k=8)
    nodes = r.retrieve("what database did we choose?")
    check("llamaindex: returns nodes-with-score", len(nodes) == 2)
    check("llamaindex: text mapped", "PostgreSQL" in nodes[0].node.get_content())
    check("llamaindex: score mapped", nodes[0].score == 0.92)
    check("llamaindex: metadata kind", nodes[0].node.metadata.get("kind") == "fact")
    REMEMBERED.clear()
    r.save("Prefer Go for backends")
    check("llamaindex: save -> remember", REMEMBERED and REMEMBERED[0]["text"] == "Prefer Go for backends")


def test_langgraph():
    try:
        from memnos_sdk.integrations.langgraph import MemnosStore
    except ImportError:
        return skip("langgraph adapter (langgraph not installed)")
    store = MemnosStore(make_client())
    items = store.search(("org", "acme"), query="database")
    check("langgraph: search -> items from recall", len(items) >= 1)
    check("langgraph: item content mapped", "PostgreSQL" in items[0].value.get("content", ""))
    REMEMBERED.clear()
    store.put(("org", "acme"), "decision-1", {"text": "use pgvector"})
    check("langgraph: put -> remember", bool(REMEMBERED) and "decision-1" in REMEMBERED[0]["text"])


def main():
    print("=== memnos_sdk framework adapters ===")
    test_langchain()
    test_llamaindex()
    test_langgraph()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
