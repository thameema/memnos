# Connecting memnos to OpenClaw

[OpenClaw](https://openclaw.ai) is a self-hosted personal AI assistant gateway (WhatsApp,
Telegram, Discord, …). Wiring memnos in gives every OpenClaw conversation **persistent,
governed long-term memory** — the assistant can recall what you told it weeks ago, across
channels, with namespace ACL on every read.

## One command

```bash
memnos agent-setup openclaw
```

This mints a scoped token and writes the memnos MCP server under `mcp.servers` in
`~/.openclaw/openclaw.json` (merging with your existing config — other servers and settings
are preserved; the file is backed up first). **Restart the OpenClaw gateway** afterward,
then verify:

```bash
openclaw mcp list          # 'memnos' should be listed
openclaw mcp probe memnos  # tools: recall, recall_wide, remember, reconcile_claim, ...
```

What it writes:

```json
{
  "mcp": {
    "servers": {
      "memnos": {
        "command": "memnos",
        "args": ["mcp"],
        "env": {
          "MEMNOS_URL": "http://127.0.0.1:8900",
          "MEMNOS_TOKEN": "mnk_...",
          "MEMNOS_NS": "user:you"
        }
      }
    }
  }
}
```

> Both memnos and OpenClaw are local-first — the MCP transport is stdio on the same
> machine, and the memnos server binds `127.0.0.1`. Nothing memory-related leaves the box
> (in free local 384-d mode, nothing leaves at all).

## Manual / CLI alternative

If you prefer OpenClaw's own CLI (it writes the same entry):

```bash
memnos token openclaw --label "openclaw gateway"   # mint a token first (shown once)
openclaw mcp set memnos '{"command":"memnos","args":["mcp"],"env":{"MEMNOS_URL":"http://127.0.0.1:8900","MEMNOS_TOKEN":"mnk_...","MEMNOS_NS":"user:you"}}'
```

## Usage notes

- OpenClaw gets the memnos MCP **tools** (`recall`, `recall_wide`, `remember`,
  `reconcile_claim`, `get_entity`, …). There are no lifecycle hooks (Claude Code only), so
  memory isn't auto-injected — the assistant calls the tools when relevant. Adding a line
  like *"Use the memnos tools to recall and store long-term facts about the user"* to your
  OpenClaw system prompt makes it proactive.
- **One namespace per deployment** is the simple default (`MEMNOS_NS`). If your OpenClaw
  serves multiple people, create a namespace per user (`memnos namespace add user:alice`)
  and grant the token accordingly — recall is ACL-clamped server-side, so a token can never
  read a namespace it wasn't granted.
- Don't put the raw token anywhere except this config — it's an opaque bearer credential
  (SHA-256 hashed at rest, instantly revocable with `memnos token` / the `/admin` console).

## Multi-server filter

If you run many MCP servers in OpenClaw and use per-server tool filters, the memnos tool
names to include are: `recall`, `recall_wide`, `remember`, `reconcile_claim`, `get_entity`,
`get_provenance`.
