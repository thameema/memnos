# Team deployment guide

How an engineering team stands up **one shared memnos** and wires each developer's agent
to it, so the whole team's agents read and write a common, governed memory.

> CLAUDE.md remembers your repo. memnos remembers your team. See
> [`docs/team-memory.md`](../team-memory.md) for the why.

---

## The mental model

memnos is a server. You run **one** instance for the team — a small VM, a box under
someone's desk, or a container — backed by **one Postgres**. Each developer's agent
points at that one host with their **own scoped bearer token**.

```
                ┌─────────────────────────────┐
   Alice's CC ──┤                             │
   Bob's Cursor─┤   one memnos server         │
   CI runner ───┤   (one Postgres + pgvector) ├── shared, governed memory
   Hermes/MCP ──┤                             │
                └─────────────────────────────┘
                   namespaces + per-token ACLs
```

There is exactly one source of truth. A fact Alice's agent learns is visible — and
correctly attributed to Alice — when Bob's agent recalls it later, even though Bob never
saw Alice's session.

This is the *cross-developer* layer. It does **not** replace CLAUDE.md / Cursor rules /
`memory.md` — those stay per-repo, per-developer, local. memnos is the shared memory on
top.

---

## Step 1 — Admin, once: provision the host

On the box that will be the team's memnos host:

```bash
# Install into an isolated environment (uv recommended; pipx works too).
uv tool install memnos        # no uv?  brew install uv  | curl -LsSf https://astral.sh/uv/install.sh | sh
# or:  pipx install memnos

# Point memnos at your team Postgres (enables pgvector, creates schema + an admin token).
memnos setup --dsn postgresql://memnos:PASSWORD@db.yourco.internal:5432/memnos

# No Postgres yet? Provision a pgvector Postgres in Docker instead:
#   memnos setup --docker

# Run it. For a long-lived host, install it as a login/boot service:
memnos autostart              # launchd/systemd service that survives reboots
# or run in the foreground under systemd/Docker:
#   memnos serve --port 8900
```

Put it behind TLS at a stable internal hostname (e.g. `https://memnos.yourco.internal`)
with your usual reverse proxy. That URL is what every developer will set as `MEMNOS_URL`.

### Create the team namespace(s)

Namespaces are how you partition and access-control memory. Structure them however your
org maps responsibility — common patterns:

- **Per team:** `team:eng`, `team:data`, `team:platform`
- **Per project:** `proj:checkout`, `proj:billing`
- **Per repo:** `repo:api`, `repo:web`

```bash
memnos namespace add team:eng --desc "Shared engineering memory"
memnos namespace add proj:checkout --desc "Checkout service decisions & incidents"
```

Grants can target an exact namespace, a **prefix** (`team:*` matches `team:eng`,
`team:data`, …), or everything (`*`). Prefix grants are how you give a lead read access
to all `team:*` at once.

---

## Step 2 — Admin, per developer: identity, token, grant

Each developer is a **principal**. Mint them a token and grant it the namespaces they
should touch. Default to least privilege.

```bash
# An identity for Alice.
memnos principal create dev-alice --kind user

# A token for her laptop. Use --ttl-days for anything that should expire (CI, contractors).
memnos token mint dev-alice --label laptop
# → prints the bearer token ONCE. Hand it to Alice over a secure channel; it is not re-shown.

# Grant read+write on the team namespace (default is read+write).
memnos grant add dev-alice team:eng

# Or grant her lead read+write across every team namespace via a prefix:
#   memnos grant add dev-alice team:*
```

### Read-only vs read+write

`memnos grant add` is read+write by default. Add `--read-only` for principals that should
consume memory but not change it:

```bash
# A dashboard / analytics service that should only read.
memnos principal create svc-reporting --kind service
memnos token mint svc-reporting --label reporting --ttl-days 90
memnos grant add svc-reporting team:* --read-only
```

Least-privilege rule of thumb: a developer gets read+write on the namespaces for the
teams/projects they're on, and nothing else. A contractor gets a short-TTL token scoped
to exactly one project namespace.

Inspect what a token can do at any time:

```bash
memnos whoami <token>          # prints the principal + its grants
```

---

## Step 3 — Each developer, on their machine: point the agent at the host

Each developer sets two env vars so their data commands and agent talk to the shared
server instead of a local one:

```bash
# ~/.bashrc (or your shell rc)
export MEMNOS_URL="https://memnos.yourco.internal"
export MEMNOS_TOKEN="<the token the admin minted for you>"
```

Then wire the agent:

```bash
# Claude Code — deterministic hook capture of both sides of every turn.
memnos agent-setup claude-code --namespace team:eng

# Cursor / Windsurf / Codex — MCP adapter (discretionary capture: the model
# decides when to save/recall).
memnos agent-setup cursor   --namespace team:eng
memnos agent-setup windsurf --namespace team:eng
memnos agent-setup codex    --namespace team:eng
```

Verify the wiring with a round-trip:

```bash
memnos remember "We use RRF to fuse pgvector + BM25 in recall." --namespace team:eng --type fact
memnos recall   "how does recall fuse search results?"          --namespace team:eng
```

`--scope wide` widens a recall across **every** namespace the token may read (useful for
a lead with `team:*`):

```bash
memnos recall "auth rotation policy" --namespace team:eng --scope wide
```

From here on, the developer's agent reads and writes the shared memory automatically.

---

## The payoff: cross-developer memory

The point of all this is the knowledge that normally never crosses between people.

1. Alice is debugging a flaky checkout deploy. Her Claude Code session decides to pin the
   retry budget and records it: her agent writes a `decision` to `team:eng` with the
   ticket IDs (`CHK-412`, `INC-89`). memnos stamps **dev-alice** as the author — server
   side, from her authenticated token, not from anything the request body claims.

2. Three weeks later Bob — who never saw Alice's session — asks his agent why the checkout
   retry budget is set the way it is. His agent recalls from `team:eng` and gets Alice's
   decision back, **attributed to dev-alice**, with the ticket IDs and the date she
   learned it.

The decision, the reasoning, and who made it travelled between two developers without a
meeting, a wiki page, or a Slack search. That is the whole product.

A short demo of this round-trip lives at
[`docs/assets/team-memory-demo.gif`](assets/team-memory-demo.gif).

---

## Governance

Everything below ships in the open-source build, not an enterprise tier.

- **Namespace ACLs.** A token reads and writes only the namespaces it is granted. Grants
  are exact, prefix (`team:*`), or `*`. `recall --scope wide` only ever searches the
  namespaces the token can already read.
- **Server-stamped authorship.** The authenticated principal is recorded as the author of
  every memory. A body-supplied author field is ignored — **you cannot spoof who learned
  something.** Attribution is trustworthy because it comes from the token, not the payload.
- **Append-only audit log.** Reads and writes are recorded, so you can see who recalled or
  wrote what. Integrations are verifiable end-to-end through it.
- **Token revocation & TTLs.** Revoke a laptop/CI/contractor token the moment it should
  stop working:

  ```bash
  memnos token ls                 # find the token id
  memnos token revoke <token-id>  # immediate
  ```

  Mint short-lived tokens for anything ephemeral:

  ```bash
  memnos token mint ci-runner   --label ci         --ttl-days 7
  memnos token mint contractor1 --label engagement --ttl-days 30
  ```

### Least-privilege examples

```bash
# Full-time engineer on the platform team: read+write on platform namespaces only.
memnos principal create dev-bob --kind user
memnos token mint dev-bob --label laptop
memnos grant add dev-bob team:platform

# CI: write to one project namespace, short TTL, rotated by re-minting.
memnos principal create ci-checkout --kind service
memnos token mint ci-checkout --label ci --ttl-days 7
memnos grant add ci-checkout proj:checkout

# Read-only auditor across everything.
memnos principal create auditor --kind user
memnos token mint auditor --label audit --ttl-days 30
memnos grant add auditor * --read-only
```

---

## Operational notes

- **Back up Postgres.** It *is* the memory. Use your normal Postgres backup/PITR; nothing
  lives outside that database.
- **One host is the source of truth.** Don't run a second instance against a different DB
  and expect them to share memory — they won't. Point every agent at the one
  `MEMNOS_URL`.
- **Fully-local / $0 mode for air-gapped teams.** No OpenAI key needed: local 384-d
  embeddings run on CPU, and fact extraction can run against any OpenAI-compatible local
  endpoint — point `MEMNOS_EXTRACT_BASE_URL` (and optionally `MEMNOS_EXTRACT_MODEL`) at
  Ollama / vLLM / LM Studio (e.g. `llama3.2:3b`). Nothing leaves your hardware and there
  is zero external API spend.

  ```bash
  export MEMNOS_EXTRACT_BASE_URL="http://localhost:11434/v1"
  export MEMNOS_EXTRACT_MODEL="llama3.2:3b"
  ```

- **Health check.** `memnos health` reports actionable findings (the platform doctor);
  `memnos status` shows server + config + embedding mode.

### What memnos is NOT

memnos is **not** a replacement for CLAUDE.md, Cursor rules, or `memory.md`. Those are
per-repo, per-developer local context and they're good at that job. memnos is the
**shared, cross-developer layer on top** — the team's memory, governed and attributed, on
your own Postgres.

---

## See also

- [`docs/team-memory.md`](../team-memory.md) — why shared memory (the concept).
- Benchmarks: LongMemEval **78.4%** (500 questions; gpt-4o answer + judge; MemoryBench
  harness), LoCoMo **64–65%** (judge ladder, strict→lenient). See the repo's benchmarks.
- [`QUICKSTART.md`](../../QUICKSTART.md) — single-machine quickstart.
