# memnos-sdk

Lightweight Python client for [memnos](https://memnos.net) — governed, vendor-neutral
**backend memory for AI agents**. Use it directly, or as a **LangChain** retriever / a
**LangGraph** long-term-memory store.

`httpx`-only (no server deps). Talks to a running memnos server over REST.

```bash
pip install memnos-sdk                  # core client
pip install 'memnos-sdk[langchain]'     # + LangChain retriever
pip install 'memnos-sdk[langgraph]'     # + LangGraph BaseStore
pip install 'memnos-sdk[all]'           # everything
```

## Core client (sync + async)

```python
from memnos_sdk import MemnosClient

with MemnosClient(base_url="http://127.0.0.1:8900", token="mnk_...", namespace="org:acme") as mem:
    mem.remember("We chose PostgreSQL + pgvector for the memory store")
    ctx = mem.context("what database did we choose?")   # ready-to-inject; no LLM at query time
    rows = mem.recall("database decision")["memories"]   # ranked memories w/ scores + dates
```

```python
from memnos_sdk import AsyncMemnosClient

async with AsyncMemnosClient(token="mnk_...", namespace="org:acme") as mem:
    await mem.remember("...")
    print(await mem.context("..."))
```

A **token** + **namespace** come from your memnos admin (`memnos token <principal>`,
`memnos grant <principal> <namespace>`). Every call is namespace-scoped and audited
server-side.

## LangChain

```python
from memnos_sdk import MemnosClient
from memnos_sdk.integrations.langchain import MemnosRetriever

retriever = MemnosRetriever(client=MemnosClient(token="mnk_...", namespace="org:acme"))
docs = retriever.invoke("auth token expiry policy")     # drop into any RAG chain
retriever.save("JWT tokens expire after 15 minutes in prod")
```

## LangGraph (long-term memory)

```python
from memnos_sdk import MemnosClient
from memnos_sdk.integrations.langgraph import MemnosStore

store = MemnosStore(MemnosClient(token="mnk_..."))
graph = builder.compile(store=store)
# in a node:  store.search(("org","acme"), query="...")  ·  store.put(("org","acme"), key, {"text": "..."})
```

memnos is *semantic* memory: `put`→remember, `search`→hybrid+reranked recall. Exact-key
`get` is best-effort (use `search`).

## API surface
`remember(text)` · `recall(query) -> {memories, context}` · `context(query) -> str` ·
`consolidate()` · `feedback(query, helpful)` · `healthy()`. Async mirror on
`AsyncMemnosClient`.

MIT.
