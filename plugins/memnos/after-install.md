# MEMNOS Plugin Installed

Welcome to **MEMNOS** — governed long-term memory and hands-free multi-agent coordination across Claude Code, Antigravity (`agy`), Gemini CLI, Cursor, and Codex.

## Quick Setup (if not already running)

```bash
uv tool install memnos && memnos setup && memnos autostart
```

## Slash Commands (`/memnos`)

- `/memnos <query>` — Recall decisions, architecture, and fixes for this repository
- `/memnos registry [topic]` — Auto-register this terminal with a human-readable name (`claude-<repo>`, `agy-<repo>`) and join or create a global cross-namespace topic channel (e.g. `/memnos registry #arch-review` or `/memnos registry payments sync`)
- `/memnos !<rule>` (or `/memnos constraint <rule>`) — Pin a permanent project rule injected into every session
- `/memnos remember <fact>` — Save a durable decision or fact
- `/memnos explain` — Inspect the exact memories, pinned rules, scores, and `file:line` code-drift notices injected into your last prompt
- `/memnos doctor` — Run platform diagnostics and automatic entity-graph cleanup
- `/memnos tips` — Show the full feature showcase and rotating workflow tips
- `/memnos ?` — Show the interactive cheat sheet

## Hands-Free Multi-Agent Coordination

- **Zero-Touch Registration:** Every active terminal running `memnos mcp` or `SessionStart` registers automatically (`same-laptop` OS PID & TTY liveness checked automatically).
- **Competing-Consumer Tasks:** Post tasks with `memnos registry #topic --task "..."` or `channel_publish(..., claimable=True)` — claimed by one listening agent, never duplicated.
- **Hands-Free Wakeup:** Run `memnos watch` in a background task to wake an idle session in `<50ms` via Postgres `LISTEN/NOTIFY` when a peer message or task arrives.
