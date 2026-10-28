# memnos plugin for Claude Code

Namespace-scoped, token-authenticated shared memory for your coding agents. This plugin wires Claude Code to a **memnos server you run yourself**: it recalls relevant memories before each prompt, saves each turn afterwards, and exposes memnos' MCP tools and the `/memnos:memnos` command.

## Install

The plugin is a thin wiring layer. It needs the `memnos` CLI and a running server first:

```bash
uv tool install memnos && memnos setup      # embedded Postgres by default, no Docker
claude plugin marketplace add thameema/memnos && claude plugin install memnos@memnos
memnos claude-setup                         # creates your scoped token once; removes older baked-in hooks
```

Skipping the last line leaves the plugin running on the admin token (writes are attributed to `admin`, not you) and any hooks an earlier `claude-setup` wrote into `settings.json` would fire alongside the plugin's.

## What the plugin does

| Hook / component | What it runs | Data it handles |
|---|---|---|
| `UserPromptSubmit` | `memnos hook recall` | Sends your prompt text, the resolved namespace and the session id to your memnos server; injects the recalled memories into context. |
| `Stop` | `memnos hook remember` | Sends your message and the assistant's reply for that turn to your memnos server for storage (agent-loop noise is skipped; secrets are redacted on ingest). |
| `SessionStart` | `scripts/session-start.sh` | Prints a one-line memory status, or a plain "memnos is not installed" message if the CLI is missing. |
| MCP server | `memnos mcp` | `recall`, `remember` and related tools, called by the model, against the same server. |

## Data handling

- **Where data goes:** to the memnos server at the URL in `~/.memnos/config.json` (default `http://127.0.0.1:8900`, on your machine). The plugin itself makes no other network calls and contains no telemetry.
- **What the server may call out to:** `api.openai.com` for embeddings and fact extraction *only if you configured an OpenAI key* (or an OpenAI-compatible endpoint of your choice via `MEMNOS_EXTRACT_BASE_URL`); otherwise embeddings run locally and nothing leaves your machine. The server also checks `pypi.org` once a day for a newer memnos version (sends no user data; disable with `MEMNOS_UPDATE_CHECK=0`).
- **What is stored locally by the CLI:** your token and settings in `~/.memnos/config.json`, a copy of the last recall per namespace, and an offline queue (`~/.memnos/offline_queue/`) of turns saved while the server is unreachable.
- **Credentials:** the plugin contains no token and no namespace. `memnos hook` and `memnos mcp` read your scoped token from `~/.memnos/config.json`.
- **Access control:** a token reaches only the namespaces it was granted (read, write or both), checked on the server; authorship is stamped from the authenticated token; every call is audit-logged.

## Requirements and limits

- The `memnos` CLI must be on `PATH` for hooks and the MCP server. GUI launches with a minimal `PATH` may not find it; use `memnos claude-setup` without the plugin there (it writes an absolute path).
- Apache-2.0. Source and docs: https://github.com/thameema/memnos and https://memnos.net.
