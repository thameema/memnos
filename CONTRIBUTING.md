# Contributing to memnos

Thanks for considering a contribution. This project moves fast and favors small,
focused PRs over large ones — a bug fix doesn't need surrounding cleanup, and a
one-shot change doesn't need a new abstraction.

## Before you start

- **Bug fixes / small improvements**: feel free to open a PR directly.
- **New features or anything that changes behavior**: please open an issue first to
  discuss the approach. Several parts of memnos (the recall ranking pipeline, the
  gateway's zero-downtime upgrade path, supersession rules) have non-obvious design
  constraints documented in code comments — worth checking those before building
  something that conflicts with them.
- Read [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — it applies to issues, PRs, and
  discussions here.

## Development setup

Postgres is a prerequisite (the engine memnos runs on, not something memnos installs
for you in dev). Easiest local path:

```bash
docker compose -f docker-compose.dev.yml up -d   # pgvector/pgvector:pg16 container

# from a source checkout:
./install.sh        # macOS/Linux
# or: .\install.ps1  # Windows

memnos setup --dsn postgresql://memnos:memnos@localhost:5433/memnos
memnos start
```

See [`QUICKSTART.md`](QUICKSTART.md) for the full walkthrough and
[`docs/guides/windows.md`](docs/guides/windows.md) for Windows-specific notes.

## Running tests

```bash
# the whole suite, against a running server (reads MEMNOS_DSN / MEMNOS_URL / MEMNOS_SECRET_KEY
# from the environment or a repo-root .env):
make test

# a single test file:
MEMNOS_DSN=postgresql://memnos:memnos@localhost:5433/memnos \
MEMNOS_URL=http://127.0.0.1:8900 \
python tests/test_memory_api.py
```

Every `tests/test_*.py` file is a standalone script that seeds its own throwaway
namespaces, asserts over real HTTP/the real control plane (no mocking of the DB layer),
and exits non-zero on the first failure. See [`tests/README.md`](tests/README.md) for
what each file covers. **Add or update a test for any behavior change** — this is a
memory system; untested changes to the write or recall path are the easiest way to
silently corrupt someone's data.

CI (`.github/workflows/ci.yml`) runs the full suite against a real `pgvector/pgvector:pg16`
container on every PR, plus CLI smoke tests on macOS/Linux/Windows and the SDK/Tommy
orchestrator unit tests. A red CI run blocks merge.

## Code style

- No comments explaining *what* code does — names should do that. A comment is only
  worth adding when it explains a non-obvious *why* (a hidden constraint, a workaround
  for a specific bug, something that would surprise a future reader).
- No speculative abstractions — don't build for a hypothetical future requirement.
- Match the existing docstring style in the module you're editing; this codebase is
  unusually thorough about documenting *why* a given implementation choice was made,
  not just what it does.
- Python 3.10+; no new runtime dependencies without a good reason (the project
  deliberately stays light — `install.sh`/`install.ps1` install fast on purpose).

## Submitting a pull request

1. Branch off `master`.
2. Keep the PR focused — one fix or feature per PR.
3. Make sure `make test` passes locally and CI is green.
4. Fill out the PR template (what changed, why, how it was tested).
5. A maintainer will review and merge (squash merge is the norm here).

## Reporting bugs / requesting features

Use the [issue templates](.github/ISSUE_TEMPLATE/) — a bug report needs clear repro
steps; a feature request should explain the problem it solves, not just the proposed
solution.

## Security issues

Please **don't** open a public issue for a security vulnerability — see
[`SECURITY.md`](SECURITY.md) for how to report it privately.
