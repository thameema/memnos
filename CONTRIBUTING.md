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

## Building & verifying release binaries

Every shipped release binary **must** be built via `scripts/build_release.sh` and pass
`scripts/release_hygiene.sh` before packaging or upload:

```bash
bash scripts/build_release.sh
bash scripts/release_hygiene.sh ./target/release/memnos
```

Never ship or tag artifacts built from an unremapped ad-hoc build — `scripts/build_release.sh`
applies cross-OS path remapping (`CARGO_ENCODED_RUSTFLAGS` covering POSIX `$HOME`, Windows
`cygpath -w` / `$USERPROFILE`, `.cargo/registry`, `.rustup`, and workspace paths), and
`scripts/release_hygiene.sh` fails the build if any builder path, test-only switch, or
internal language branding appears in the binary or distribution archives.

## Try it safely (isolated local trial — zero touch to a live install)

To test a newly built binary end-to-end on a machine that already runs a live `memnos`
instance (for example on `:8900` against `localhost:5432/memnos` and `~/.memnos`), isolate
four things: **Postgres**, **`HOME`**, **port**, and **working directory** (`MEMNOS_NO_DOTENV=1`
prevents any repo `.env` from overriding trial settings, and `--require-empty-db` aborts
before any DDL if `MEMNOS_DSN` ever points at an existing database):

```bash
# 1. Build the candidate binary and move into a clean scratch directory + HOME
BIN="$PWD/target/release/memnos"
TRIAL_HOME="$(mktemp -d /tmp/memnos-trial.XXXXXX)"
cd "$TRIAL_HOME"

# 2. Start a disposable pgvector container on an unused port (:5434)
docker run -d --name memnos-trial-pg \
  -e POSTGRES_USER=memnos \
  -e POSTGRES_PASSWORD=memnos \
  -e POSTGRES_DB=memnos_trial \
  -p 127.0.0.1:5434:5432 \
  pgvector/pgvector:pg16

# (Optional: reuse an existing HuggingFace cache read-only so you skip the ~930 MB model download
#  without touching ~/.memnos; omit HF_HOME to test a cold download into $TRIAL_HOME/.memnos/models)
export HOME="$TRIAL_HOME"
export MEMNOS_NO_DOTENV=1
export MEMNOS_DSN="postgresql://memnos:memnos@127.0.0.1:5434/memnos_trial"
export MEMNOS_PORT=8920

# 3. Initialize the empty trial DB + $TRIAL_HOME/.memnos/config.json (--require-empty-db refuses
#    immediately if the target database already has memnos schemas)
"$BIN" setup --dsn "$MEMNOS_DSN" --port "$MEMNOS_PORT" --local --require-empty-db --yes
# (Or pass `--openai-key sk-...` instead of `--local`.)

# 4. Run the server in the foreground and verify the startup banner shows ONLY $TRIAL_HOME and :5434
"$BIN" serve --port "$MEMNOS_PORT" &
TRIAL_PID=$!
# Expected startup output:
#   [memnos] config: /tmp/memnos-trial.XXXXXX/.memnos/config.json (present)
#   [memnos] target db: 127.0.0.1:5434/memnos_trial
#   [memnos] server v... listening on http://127.0.0.1:8920 (db=127.0.0.1:5434/memnos_trial, config=...)

# 5. Exercise CLI, memory, recall, and coordination against :8920 (wait until ready=true)
"$BIN" status
"$BIN" remember "Isolated trial decision: keep staging isolated on port 8920" --namespace proj:trial --no-extract
"$BIN" recall "staging isolated port" --namespace proj:trial
"$BIN" session register trial-agent --namespace proj:trial
"$BIN" inbox send trial-agent "Hello from isolated trial" --session trial-peer --subject "Smoke check" --namespace proj:trial
"$BIN" inbox ls --session trial-agent --namespace proj:trial

# 6. Teardown (leaves live :8900, localhost:5432/memnos, and real ~/.memnos untouched)
kill "$TRIAL_PID" && wait "$TRIAL_PID" 2>/dev/null || true
docker rm -f memnos-trial-pg
rm -rf "$TRIAL_HOME"
```

> **Note on editor / CLI hooks:** Global Claude Code or editor hooks configured under your real
> home directory (`~/.claude`, `~/.gemini`, etc.) continue talking to your live `:8900` server.
> If you also want to test Claude Code hooks/MCP against the trial server without touching your
> live setup, export `CLAUDE_CONFIG_DIR="$TRIAL_HOME/.claude"` before running `"$BIN" setup claude-code`.

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
