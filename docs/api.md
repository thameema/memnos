# memnos REST API reference

Human-readable companion to the machine-readable contract in
[`openapi.yaml`](../openapi.yaml) (OpenAPI 3.1). The spec is **CI-enforced**:
`tests/test_openapi_contract.py` exercises every documented operation against a real
server and validates the responses against the spec schemas — so this surface cannot
silently drift.

- Base URL: `http://127.0.0.1:8900` (configurable; the server binds localhost only)
- Auth: `Authorization: Bearer mnk_...` on everything except `/healthz` and `/readyz`
- All data-plane endpoints are `POST` with a JSON body that includes `"namespace"`
- The **capture proxy** (`memnos proxy`, :8910) is a separate relay process and is
  deliberately outside this contract

```bash
TOKEN=mnk_...                                   # memnos token mint <principal>
M="http://127.0.0.1:8900"
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
```

```python
# pip install memnos-sdk
from memnos_sdk import MemnosClient
m = MemnosClient("http://127.0.0.1:8900", token="mnk_...", namespace="proj:myapp")
```

## Error taxonomy

Every error body is `{"error": "...", "msg"?: "..."}`.

| code | meaning |
|---|---|
| 400 | malformed JSON / missing or oversized field |
| 401 | missing, invalid, revoked, or expired token |
| 403 | namespace outside the token's grants (admin plane: non-admin token). Audited. |
| 404 | unknown route, or object not in this namespace |
| 409 | admin secrets only: vault locked (`MEMNOS_SECRET_KEY` unset) |
| 413 | body > 256 KB (`/ingest/file`: extracted text > 2 MB) |
| 415 | `/ingest/file`: can't extract text from the upload |
| 429 | **not used** — no built-in rate limiting; front with a reverse proxy if needed |
| 500 | internal fault (audited; details never leaked) |
| 503 | `{"error": "database unreachable — is Postgres running?"}` — fail fast |

## Health

| endpoint | what |
|---|---|
| `GET /healthz` | liveness (no auth) → `{"ok": true}` |
| `GET /readyz` | DB reachable (no auth) → `{"ready": true}` or 503 |
| `GET /metrics` | 24h per-op reliability rollup (any valid token) |

```bash
curl -s $M/healthz
curl -s "${H[@]}" $M/metrics
```

## Memory

### `POST /remember` (alias: `POST /memory/write`)

Store a turn; in OpenAI mode the server also extracts bi-temporal facts. The memory's
**author** is the authenticated principal — never client-supplied. `"async": true`
returns right after the raw turn is stored and queues extraction
(`{"turn_id": …, "facts": null, "extraction": "queued"}`).

```bash
curl -s "${H[@]}" $M/remember -d '{
  "namespace": "proj:myapp",
  "text": "We chose Postgres 16 for staging.",
  "speaker": "user", "session_id": "s-42", "async": true}'
```

```python
m.remember("We chose Postgres 16 for staging.", speaker="user")
```

### `POST /recall` · `POST /memory/search` · `POST /recall_v2` · `POST /memory/context`

Hybrid retrieval + cross-encoder rerank. `/recall` returns `memories` + a paste-ready
`context`; `/memory/search` (and its `/recall_v2` alias) return memories only;
`/memory/context` the context only. Optional: `scope: "all"` (widen across every
readable namespace; adds `namespaces_searched`), `author` (filter by writer),
`raw_quota` / `fact_quota` / `max_chars`.

**Grounded recall:** when an admin has linked the namespace to knowledge namespaces
(`memnos namespace link src dst`), recall also searches the linked namespaces the
caller may read — `grounded_in` lists those used, `links_skipped` those denied (link =
policy, grant = permission; both required). The keys appear only when links exist.

```bash
curl -s "${H[@]}" $M/recall -d '{"namespace": "proj:myapp", "query": "staging DB decision?"}'
```

```python
out = m.recall("staging DB decision?")        # {"memories": [...], "context": "..."}
print(m.context("staging DB decision?"))      # just the block
```

### `POST /memory/delete`

Expire a fact by id (system-time delete; history is preserved). 404 if not in this
namespace.

```bash
curl -s "${H[@]}" $M/memory/delete -d '{"namespace": "proj:myapp", "id": 123}'
```

### `POST /consolidate`

Offline dossier-building LLM pass over a namespace's facts → `{"dossiers": n, "inferred": n}`
(0 in local mode — needs an LLM). `inferred` counts LLM-derived conclusions written when
`MEMNOS_INFER_ON_SLEEP=1` is set (opt-in; 0 otherwise).

```python
m.consolidate()
```

### `POST /feedback`

`{"namespace", "query", "helpful": true|false, "note"?}` — the true recall-quality
signal. → `{"ok": true}`

```python
m.feedback("staging DB decision?", helpful=True)
```

### `POST /reconcile`

Check an external claim (e.g. a local note) against memory:
`{"namespace", "statement", "subject"?, "predicate"?}` →
`{"claim", "matches": [...], "conflicts": [...], "stale": bool}`.

```bash
curl -s "${H[@]}" $M/reconcile -d '{
  "namespace": "proj:myapp", "statement": "Staging runs Postgres 15",
  "subject": "staging", "predicate": "runs"}'
```

### `POST /ingest/file`

Chunk + store a document. Send extracted `text`, or `content_b64` (PDF/DOCX need the
optional `pypdf`/`python-docx`). `"extract": true` also runs per-chunk fact extraction.
→ `{"filename", "chunks", "turn_ids"}`

```python
m.ingest_file("notes.md", open("notes.md").read())
```

### `POST /namespace/copy`

`{"namespace": <DESTINATION>, "src", "mode": "copy"|"move", "like"?}` — needs write on
the destination + read on the source. → counts `{mode, src, dst, raw_turns, facts, episodes}`

## Graph

| endpoint | body | returns |
|---|---|---|
| `POST /entity` | `{namespace, name, depth?}` | entity + neighbours + facts (404 unknown) |
| `POST /provenance` | `{namespace, id}` | fact + verbatim source turns |
| `POST /related` | `{namespace, name}` | weight-ranked adjacency |
| `POST /graph` | `{namespace, entities[]\|name, hops?, limit?}` | facts over the N-hop reachable set |
| `POST /community` | `{namespace, name}` | the entity's connected component |
| `POST /contradictions` | `{namespace}` | same subject+predicate, >1 current object |
| `POST /knowledge/health` | `{namespace}` | 0-100 structural score + counts |

```bash
curl -s "${H[@]}" $M/entity -d '{"namespace": "proj:myapp", "name": "Ada", "depth": 2}'
```

## Episodes

| endpoint | body | returns |
|---|---|---|
| `POST /episode/segment` | `{namespace, gap_minutes?}` | `{"episodes": n}` created |
| `POST /episode` | `{namespace, id}` | episode + turns + derived facts |
| `POST /episode/recall` | `{namespace, query, k?}` | semantic episode search |
| `POST /episode/decay` | `{namespace, half_life_days?}` | `{"updated": n}` salience recompute |

## Corpus (architecture constraints)

| endpoint | body | returns |
|---|---|---|
| `POST /corpus/ingest` | `{namespace, name, text, kind?, git_sha?}` | RFC-2119 constraints stored |
| `POST /corpus/check` | `{namespace, snippet\|code}` | constraints relevant to a snippet |
| `POST /corpus/list` | `{namespace}` | registered sources |

```bash
curl -s "${H[@]}" $M/corpus/ingest -d '{
  "namespace": "proj:myapp:arch", "name": "ARCHITECTURE.md",
  "text": "Services MUST validate input. Tokens SHALL NOT be logged."}'
```

## Pub/sub

| endpoint | body | returns |
|---|---|---|
| `POST /subscribe` | `{namespace, webhook?}` | `{subscription_id, cursor, …}` |
| `POST /feed` | `{namespace, subscription_id, limit?}` | new turns since the cursor (cursor advances) |
| `POST /unsubscribe` | `{namespace, subscription_id}` | `{"unsubscribed": bool}` |

Webhook subscriptions get pushed batches (at-least-once) by the background pusher;
`POST /admin/api/deliver` runs a pass on demand.

## Admin plane (`/admin/api/*` — `*`-grant token required)

| method + path | what |
|---|---|
| `GET/POST/DELETE /admin/api/namespaces` | census / register (`{name, description?, kind?}`) / delete (`?name=&purge=1`) |
| `GET/POST/DELETE /admin/api/namespaces/links` | grounded-recall links: list (`?ns=`) / link `{src,dst}` / unlink (`?src=&dst=`) |
| `POST /admin/api/namespaces/kind` | `{name, kind: memory\|knowledge}` |
| `GET/POST /admin/api/principals` | list / create `{name, kind?}` |
| `GET/POST /admin/api/tokens` | metadata (`?principal=<id>`) / mint `{principal_id, label?, ttl_days?}` — plaintext returned ONCE |
| `POST /admin/api/tokens/revoke` | `{id}` |
| `GET/POST/DELETE /admin/api/grants` | list (`?principal=`) / grant `{principal_id, namespace, can_read?, can_write?}` / revoke (`?principal=&namespace=`) |
| `GET /admin/api/stats` | reliability rollup (`?hours=`, 1–720) |
| `GET /admin/api/usage` | token/cost rollup (`?hours=` optional, default all-time) |
| `GET /admin/api/audit` | paginated audit log (`?limit=&offset=`; `total` is a planner estimate) |
| `GET /admin/api/health` | actionable findings (the "doctor") |
| `GET /admin/api/quality` | stale-suppression canary trend |
| `GET /admin/api/subscriptions` | subscriptions (`?principal=`) |
| `POST /admin/api/deliver` | run a webhook delivery pass now |
| `GET /admin/api/provider` | embedding/extraction mode + vault state |
| `GET/POST/DELETE /admin/api/secrets` | vault: list metadata / store `{name, value, description?}` / delete (`?name=`) — plaintext never returned; 409 when locked |

```bash
ADM=mnk_...   # admin token (memnos setup prints one)
curl -s -H "Authorization: Bearer $ADM" "$M/admin/api/namespaces"
curl -s -H "Authorization: Bearer $ADM" -H "Content-Type: application/json" \
  $M/admin/api/namespaces/links -d '{"src": "proj:myapp", "dst": "proj:myapp:arch"}'
```
