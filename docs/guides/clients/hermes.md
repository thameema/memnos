# Connecting memnos to Hermes Agent (Nous Research)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) speaks MCP natively as a
client (since v0.2.0). Wiring memnos in gives Hermes **persistent, governed long-term
memory** that survives across sessions and model providers — and because Hermes runs
self-hosted/open models, pairing it with memnos in **free local 384-d mode** gives you a
fully private memory stack where nothing leaves your machine.

## One command

```bash
memnos agent-setup hermes
```

This mints a scoped token and adds memnos under `mcp_servers` in `~/.hermes/config.yaml`
(merging with your existing YAML — other servers and settings are preserved; the file is
backed up first). Then **run `/reload-mcp` inside Hermes** (or restart it) and the memnos
tools appear in the tool list.

What it writes:

```yaml
mcp_servers:
  memnos:
    command: memnos
    args: [mcp]
    env:
      MEMNOS_URL: http://127.0.0.1:8900
      MEMNOS_TOKEN: mnk_...
      MEMNOS_NS: user:you
```

## Manual alternative

Mint a token yourself and add the block above by hand:

```bash
memnos token hermes --label "hermes agent"     # prints the token ONCE — copy it
```

Hermes' own CLI works too:

```bash
hermes mcp add memnos --command memnos --args mcp
# then add the env block (URL/TOKEN/NS) to the generated entry in ~/.hermes/config.yaml
hermes mcp test memnos
```

## Usage notes

- Hermes gets the memnos MCP **tools** (`recall`, `recall_wide`, `remember`,
  `reconcile_claim`, `get_entity`, `get_provenance`). There are no lifecycle hooks
  (Claude Code only), so memory isn't auto-injected — Hermes calls the tools when relevant.
  A system-prompt nudge like *"Use the memnos tools to recall context before answering and
  to store durable facts"* makes it consistent.
- If you use Hermes' per-server **tool filters**, whitelist with:

  ```yaml
  mcp_servers:
    memnos:
      # ...
      tools:
        include: [recall, recall_wide, remember, reconcile_claim, get_entity]
  ```
- **Recall has no LLM in the loop** — it's hybrid Postgres search + a local ONNX reranker —
  so adding memory doesn't add a second model call or provider dependency to Hermes' turn.
- Namespaces are explicit: `MEMNOS_NS` scopes what this Hermes instance reads/writes, and
  the token is ACL-clamped server-side. One namespace per agent (or per user) keeps
  memories cleanly separated; use `recall_wide` to search across all namespaces the token
  may read.
