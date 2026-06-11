# memnos tests

Integration tests for the memnos server + governance control plane. Every test seeds the
database directly and asserts behaviour over HTTP and the control plane — **no LLM and no
OpenAI key are required** (the server runs in free local-384 embedding mode in CI). Each
file is a standalone script that exits non-zero on the first failure, and cleans up the
throwaway namespaces it creates.

## What's covered

| File | Area |
|------|------|
| `test_admin_api.py` | admin bootstrap, principals, tokens, grants |
| `test_vault.py` | encrypted secret vault, key rotation, redaction |
| `test_cli.py` | the `memnos` CLI end-to-end (subprocess) |
| `test_memory_api.py` | graph reads, memory CRUD, context block |
| `test_knowledge_api.py` | entity / related / community / contradictions / health |
| `test_pubsub_api.py` / `test_pubsub_push.py` | subscriptions + webhook delivery |
| `test_corpus_api.py` | corpus ingest + constraint enforcement |
| `test_provenance_api.py` | fact ↔ source-turn provenance |
| `test_ingest_api.py` | file ingest → chunked raw turns |
| `test_episodic_api.py` | episode segmentation, recall, decay |
| `test_migrate_api.py` | copy / move memories between namespaces |
| `test_reconcile_api.py` | local-vs-remote staleness reconciliation |
| `test_wide_recall_api.py` | cross-namespace (`scope:"all"`) recall |
| `test_author_attribution.py` | server-stamped `author_principal` + `(by ...)` context tags + author filter |
| `test_grounded_recall.py` | knowledge namespaces, namespace links, grounded `/recall` fan-out |
| `test_memory_types.py` | typed memories (`type` on write/recall), pinned constraint injection, admin memory feed |
| `test_supersession.py` / `test_supersession_matrix.py` | bi-temporal write-path supersession: SPO, negation close-out, dedupe, backdating, value-update cues, quantified-object rule |
| `test_recall_staleness.py` | stale RAW TURNS in recall — `superseded`/`superseded_at` annotation + demotion below current facts |
| `test_broad_query_ranking.py` | broad-query recall tune — query-specificity heuristic, facts-first order on broad questions, turn length normalization, restatement/salience boost, kill switches |
| `test_namespace_reconcile.py` | `memnos namespace reconcile` backfill: dry-run counts, close/dedupe, idempotency, `--limit` |

## Running locally

You need a running memnos server and a Postgres it can reach. The tests honour two env
vars (with sensible local defaults):

- `MEMNOS_DSN` — Postgres connection (must match the server's)
- `MEMNOS_URL` — server base URL (default `http://127.0.0.1:8900`)
- `MEMNOS_SECRET_KEY` — must match the server's vault key (for `test_vault` / `test_cli`)

```bash
# from the repo root, with the server already running:
make test

# or run a single test:
MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos \
MEMNOS_URL=http://127.0.0.1:8900 \
python tests/test_memory_api.py
```

`make test` runs every `tests/test_*.py` from the repo root and reports a pass/fail tally.

## CI

`.github/workflows/ci.yml` runs the whole suite on every push to `dev`/`main` and on every
pull request, against a `pgvector/pgvector:pg16` service in free local-384 mode. A red suite
blocks the merge.
