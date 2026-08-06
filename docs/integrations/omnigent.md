# memnos × Omnigent (server-wide response capture)

Automatic, deterministic capture of every Omnigent-orchestrated agent's assistant
responses into memnos — **with zero changes to Omnigent itself**. This uses Omnigent's
own, unmodified extension point (a server-wide `type: function` policy, resolved via a
dotted Python import path), not a fork or a patch.

**Read this before wiring it up — the guarantee level, stated plainly:**

- **One-directional. Write-only.** This captures assistant responses *into* memnos. It
  does **not** recall or inject memory *into* an Omnigent conversation — Omnigent has no
  turn-boundary hook for that today, for any harness. Pair it with your agent's own
  `recall`/MCP calls (or the `memnos agent-setup omnigent --agent-dir <path>` command —
  a *different*, older command, see [below](#not-to-be-confused-with-agent-setup-omnigent))
  if you also want recall.
- **Fails open, always.** A memnos outage, a bad token, a network timeout — none of it
  ever blocks, denies, or delays the user-visible assistant response. See
  [Failure behavior](#failure-behavior-and-why-it-matters) for exactly why this had to be
  designed in, not just hoped for.
- **Coverage is narrower than "every assistant response."** See
  [What actually gets captured](#what-actually-gets-captured) — there is a real,
  documented gap for one class of Omnigent session.

---

## Quick start

```bash
pip install memnos-sdk               # in the OMNIGENT SERVER's own Python environment

# Embedded: memnos and the omnigent server run on the same machine.
memnos server-setup omnigent --config /path/to/omnigent-server-config.yaml --mode embedded

# Central/team: the omnigent server talks to a remote, shared memnos over HTTP.
export MEMNOS_URL=https://memnos.yourco.internal
export MEMNOS_TOKEN=<a token an admin minted for this server — see docs/guides/team.md>
memnos server-setup omnigent --config /path/to/omnigent-server-config.yaml --mode central
```

Then restart the server with that same config: `omnigent server --config /path/to/omnigent-server-config.yaml`.

The `--config` path is **the file you pass to `omnigent server --config/-c`** (or, for a
Docker/hosted deploy, whatever `$OMNIGENT_CONFIG` points at) — not
`~/.omnigent/config.yaml`'s `default_agent` registry, which is a different file with a
different schema (see the callout below). The command requires this path explicitly
(or `$OMNIGENT_CONFIG`) rather than guessing, specifically to avoid that mix-up.

## Not to be confused with: `agent-setup omnigent`

memnos already has an older, unrelated command, `memnos agent-setup omnigent
--agent-dir <path>`, which wires memnos as an MCP tool (`recall`/`remember`) into **one**
Omnigent **agent's** `config.yaml`, so that agent can explicitly call memnos when it
chooses to. That's *discretionary*, *per-agent*, and *pull*-based (the model decides
when to call it).

`server-setup omnigent` (this feature) is the opposite on every axis: *deterministic*
(fires on every covered turn, no model discretion), *server-wide* (one edit to the
server's own config covers every agent Omnigent runs, no per-agent wiring), and
*push*-based (memnos never has to be called — Omnigent's policy engine calls memnos).
Run either, both, or neither; they don't conflict.

---

## How it works

Omnigent's server `--config` YAML supports a top-level `policies:` block
(`omnigent/spec/parser.py` `parse_default_policies`, applied server-wide to every
agent/session). A `type: function` policy resolves its `handler:` field as a plain
dotted Python import path (`module.sub.attr` — split on the *last* dot, no `module:attr`
colon form) via `importlib.import_module` + `getattr`
(`omnigent/policies/function.py` `_resolve_dotted_path`). `server-setup omnigent` writes:

```yaml
policies:
  memnos_capture:
    type: function
    handler: memnos_sdk.integrations.omnigent.capture_response
    config:
      memnos_namespace: agent:omnigent      # or your --namespace override
      memnos_url: http://127.0.0.1:8900     # embedded mode only — central mode omits
                                             # this and relies on the server process's
                                             # own $MEMNOS_URL at runtime
```

Because the handler is just an importable Python function, this needs **no Omnigent
source changes** — only `pip install memnos-sdk` in whatever Python environment the
`omnigent server` process runs in. `memnos_sdk` (not the full `memnos` package) is
exactly the lightweight, httpx-only client library this repo already publishes for
third-party frameworks — the same one the LangChain/LangGraph/LlamaIndex adapters use
(`sdk/memnos_sdk/integrations/`). It never needs Postgres drivers, embedding models, or
any of the full server's dependencies inside the Omnigent process.

When an assistant turn is evaluated, Omnigent calls
`capture_response(event, config)` with the full, untruncated assistant text at
`event["data"]`. The handler:

1. Extracts the text and, if present, the calling actor's identity
   (`event["context"]["actor"]["run_as"]` or `"client_id"`) for attribution.
2. Resolves `memnos_url` / `memnos_namespace` from the policy's `config:` block, falling
   back to `MEMNOS_URL` / `MEMNOS_NS` env vars, falling back to
   `http://127.0.0.1:8900` / `agent:omnigent`.
3. Reads the bearer token **from the `MEMNOS_TOKEN` environment variable only** — never
   from the YAML. `server-setup omnigent` never writes a token into the config file (that
   file is operator-editable and often world-readable); it prints an
   `export MEMNOS_TOKEN=...` line instead.
4. Fires the write on a short-lived background thread and returns an ALLOW verdict
   **immediately**, without waiting for the write to finish.

## Failure behavior and why it matters

Omnigent's function-policy engine has a documented fail-closed contract: if a policy
callable *raises*, the engine converts that into a **DENY** on the assistant's response —
replacing the user-visible text with a deny sentinel
(`omnigent/runtime/policies/engine.py`, the wrapper around `policy.evaluate()`). A memory
side-channel must never be able to do that to a real conversation. `capture_response`
is written so it **cannot raise** under any failure — bad event shape, unreachable
memnos, expired token, whatever — and it **always returns ALLOW**.

The write itself also runs on a background thread, not inline: `_evaluate_output_policy`
is awaited *before* the assistant message is persisted, i.e. in the response's own
critical path. Blocking there on memnos's latency (or a timeout) would add real,
user-visible delay to every single turn. Firing the write on a daemon thread and
returning immediately makes that latency effectively zero, regardless of whether memnos
is fast, slow, or completely down. (Failures are logged, not surfaced back into the
conversation — check the Omnigent server's own logs if writes aren't landing.)

## What actually gets captured

The capture policy fires on Omnigent's `_evaluate_output_policy`, which runs for
`type: "message", role: "assistant"` events posted through the standard Agent Platform
events API — i.e. whenever Omnigent's own task/runner execution finishes a turn and
reports the result back through the normal path
(`omnigent/server/routes/sessions/routes_events.py`).

**It does not fire** for Omnigent's separate `external_assistant_message` event type,
used to *mirror* assistant text that came from a process running **outside any Omnigent
task** (e.g. displaying output from a raw external terminal session Omnigent didn't
itself orchestrate). That path calls `_persist_external_assistant_message`, which its own
docstring says "intentionally bypasses the legacy persist path" — including policy
evaluation. Those turns are not captured by this feature. If your deployment relies
heavily on that mirroring path rather than Omnigent-run agent tasks, this feature's
real-world coverage will be narrower than "every assistant response" — verify against
your own setup before depending on it for compliance/audit purposes.

## Attribution and namespace

Omnigent hosts many different agents server-wide, and the RESPONSE-phase event Omnigent
hands to a function policy carries no per-agent or per-session identifier (no agent
name, no conversation ID) — only the calling actor's identity, when known
(`event["context"]["actor"]`). There is no natural 1:1 mapping to memnos's
`user:<name>` convention here. The default namespace is therefore **`agent:omnigent`** —
one shared bucket for everything this server captures, mirroring the same
`agent:<name>` convention memnos already uses for other autonomous-agent integrations
(Hermes, OpenClaw). Override it with `--namespace` if you want a different bucket (e.g.
`agent:omnigent-prod`); every captured turn from that server config still lands in one
namespace, not one per Omnigent agent.

Captured turns are stored with `speaker="assistant"` (not `"user"`) — this is the
agent's own output, not a claim being attributed to a human.

## `--mode embedded` vs `--mode central`

Both modes use the exact same code path — an HTTP call to whatever memnos server
`MEMNOS_URL` resolves to. The mode only changes how the token is obtained and whether a
concrete `memnos_url` gets baked into the generated YAML:

| | `--mode embedded` (default) | `--mode central` |
|---|---|---|
| Where memnos runs | same machine as the omnigent server | a remote/shared memnos (docs/guides/team.md) |
| Token | minted via direct Postgres access (or a pre-set `$MEMNOS_TOKEN`, used verbatim) | must already be set as `$MEMNOS_TOKEN` — never minted here (no DB access assumed) |
| `memnos_url` in the YAML | baked in (`http://127.0.0.1:<port>`) | omitted — the server process's own `$MEMNOS_URL` decides at runtime |

`server-setup omnigent` is idempotent (re-running without `--force` is a no-op once
wired), merges into any existing `policies:` block without touching other policies
already there, and backs up the config file before writing.

## Verifying it's working

```bash
memnos recall "<something the agent recently said>" --namespace agent:omnigent
```

If nothing comes back: confirm `memnos_sdk` is importable in the omnigent server's own
Python environment (`pip show memnos-sdk` there, not just wherever you ran
`server-setup`), confirm `MEMNOS_TOKEN` is actually set in that process's environment,
and check the omnigent server's own logs for `omnigent capture:` warning lines — failures
are logged there, by design, rather than surfaced anywhere in the conversation.
