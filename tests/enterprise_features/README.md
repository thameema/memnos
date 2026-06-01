# Enterprise-feature regression suite

E2E + regression tests for the local enterprise build features.
Targets the **dev** memnos stack on `localhost:19766` — production
memnos on `:8766` is never touched.

## Coverage

| File | Feature |
|---|---|
| `test_feature1_embedder.py` | FastEmbed/ONNX with nomic-embed-text-v1.5 |
| `test_feature2_episodes.py` | Immutable Episodes + `source_episode_ids` lineage |
| `test_feature3_hybrid_search.py` | Vector + BM25 + RRF fusion |

## Running

Prereqs: dev stack is up (`/tmp/memnos-dev-data/dev.sh up -d`).

```bash
pytest -p no:flask tests/enterprise_features/ -v
```

The `-p no:flask` is a local workaround for a broken `pytest-flask`
install in the Homebrew Python and is not required in CI.

## Double-pass safety

Every test allocates a fresh, unique namespace (`regr-<label>-<unix>-<uuid8>`)
on each invocation. Running the suite back-to-back never collides — the only
shared state is the dev memnos process itself, which is idempotent.

## Environment

| Var | Default |
|---|---|
| `MEMNOS_DEV_URL` | `http://localhost:19766` |
| `MEMNOS_DEV_API_KEY` | parsed from `/tmp/memnos-dev-data/.env` |

If the dev stack isn't reachable the suite **skips** rather than fails so
it can stay in the standard pytest run without flaking CI.
