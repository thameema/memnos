# engram-sdk

Python SDK for [engram](https://github.com/thameema/engram) — persistent memory for AI agents.

```python
from engram_sdk import EngramClient

client = EngramClient(base_url="http://localhost:8766", api_key="your-key")

client.write("Chose PostgreSQL for the event store — ACID guarantees required",
             namespace="org:myproject",
             memory_type="decision",
             affects=["event-store", "payment-service"],
             rationale="Transactional writes across tables need ACID; Redis/Kafka can't provide this.")

results = client.search("event store choice", namespace="org:myproject")
for r in results:
    print(r.content)
```

## Install

```bash
pip install engram-sdk
```

With LangChain or LlamaIndex integrations:

```bash
pip install "engram-sdk[langchain]"
pip install "engram-sdk[llamaindex]"
pip install "engram-sdk[all]"
```

Requires Python 3.10+.

## Quick start

### Sync client

```python
from engram_sdk import EngramClient

client = EngramClient(
    base_url="http://localhost:8766",
    api_key="engram-local-dev-key",
)

# Write a memory
mem = client.write(
    content="Use ArcadeDB as the sole graph store — single process handles vector + graph + docs",
    namespace="org:myproject",
    memory_type="decision",
    affects=["storage", "arcadedb"],
    rationale="Eliminates three-service ops complexity (Neo4j + Qdrant + Graphiti).",
    tags=["architecture", "adr"],
)
print(mem.id)

# Search across all accessible namespaces
results = client.search("storage architecture decision")
for r in results:
    print(f"[{r.score:.2f}] {r.content[:80]}")

# Get a specific memory
mem = client.get(mem.id)

# Delete a memory
client.delete(mem.id)
```

### Async client

```python
import asyncio
from engram_sdk import AsyncEngramClient

async def main():
    async with AsyncEngramClient(base_url="http://localhost:8766", api_key="key") as client:
        await client.write("Chose gRPC for inter-service comms", namespace="org:svc",
                           memory_type="decision", affects=["api-gateway"])
        results = await client.search("gRPC decision")
        for r in results:
            print(r.content)

asyncio.run(main())
```

## Memory types

| Type | When to use |
|------|-------------|
| `decision` | Architectural choices — what was decided and why |
| `constraint` | Rules that apply to a component — enforced at code review |
| `adr` | Architecture Decision Records |
| `fact` | Context, session notes, anything else |
| `session` | Automated session summaries from hooks |

```python
from engram_sdk import MemoryType

client.write("...", namespace="org:x", memory_type=MemoryType.DECISION)
```

## Corpus — architecture docs as a CI gate

Register a folder of markdown ADRs/decision docs and query them as constraints:

```python
from engram_sdk import EngramClient

client = EngramClient(base_url="http://localhost:8766", api_key="key")

# Register a corpus (one-time setup)
corpus = client.corpus.register(
    name="backend-adrs",
    source="./docs/architecture",
    namespace="org:myproject",
)

# Check a code change against registered constraints
result = client.corpus.check(
    corpus_id=corpus.id,
    component="payment-service",
    code_snippet="db.execute('INSERT INTO payments ...')",
)
for hit in result.violations:
    print(f"[{hit.severity}] {hit.rule}")
```

## LangChain integration

```python
from langchain_openai import ChatOpenAI
from langchain.chains import ConversationChain
from engram_sdk.integrations.langchain import EngramMemory

memory = EngramMemory(
    base_url="http://localhost:8766",
    api_key="your-key",
    namespace="org:myproject",
    top_k=5,
)

chain = ConversationChain(llm=ChatOpenAI(), memory=memory)
response = chain.predict(input="What storage decisions have we made?")
```

`EngramMemory` loads the top-k most relevant memories as conversation context and writes new exchanges back to engram automatically.

## LlamaIndex integration

```python
from llama_index.core import VectorStoreIndex
from engram_sdk.integrations.llamaindex import EngramReader

reader = EngramReader(base_url="http://localhost:8766", api_key="your-key")
documents = reader.load_data(namespace="org:myproject", query="architecture decisions", top_k=20)

index = VectorStoreIndex.from_documents(documents)
query_engine = index.as_query_engine()
print(query_engine.query("Which storage system did we pick and why?"))
```

## Governance queries

```python
# Get all decisions governing a component
decisions = client.get_governing_decisions(namespace="org:myproject", component="auth-service")
for d in decisions:
    print(f"[{d.memory_type}] {d.content[:80]}")
    print(f"  WHY: {d.rationale}")

# Get all constraints in a namespace
constraints = client.get_constraints(namespace="org:myproject")
```

## Namespace export / import

```python
# Export
data = client.export_namespace("org:myproject")
import json
json.dump(data, open("backup.json", "w"), indent=2)

# Import into a new namespace
with open("backup.json") as f:
    client.import_namespace("org:newproject", json.load(f))
```

## Error handling

```python
from engram_sdk.exceptions import NotFoundError, AuthenticationError, EngramError

try:
    mem = client.get("nonexistent-id")
except NotFoundError:
    print("Memory not found")
except AuthenticationError:
    print("Check your API key")
except EngramError as e:
    print(f"Engram error: {e}")
```

| Exception | When raised |
|-----------|-------------|
| `EngramError` | Base class for all SDK errors |
| `AuthenticationError` | Invalid or missing API key |
| `NotFoundError` | Memory or corpus not found |
| `ValidationError` | Malformed request (bad namespace, etc.) |
| `ServerError` | 5xx from the engram server |
| `ConnectionError` | Server unreachable |

## Configuration

```python
client = EngramClient(
    base_url="http://localhost:8766",  # or ENGRAM_API env var
    api_key="your-key",               # or ENGRAM_KEY env var
    timeout=15.0,                     # per-request timeout in seconds
)
```

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGRAM_API` | `http://localhost:8766` | Server URL |
| `ENGRAM_KEY` | — | API key |

## Running engram locally

```bash
git clone https://github.com/thameema/engram
cd engram
docker compose up -d
# Server is at http://localhost:8766, key: engram-local-dev-key
```

See the [main README](../../README.md) for full setup.
