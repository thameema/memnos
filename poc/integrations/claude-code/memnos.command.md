# /memnos slash command — sample

Copy this to `~/.claude/commands/memnos.md` (replace `mnk_...` with your token and the
namespace with yours). Then `/memnos <query>` recalls from memnos and answers from it;
`/memnos` alone shows server status. Slash commands are picked up immediately (no restart).

```markdown
---
description: memnos memory — recall a query or show status
allowed-tools: Bash(curl:*), Bash(python3:*)
---

Server health: !`curl -s -m3 http://127.0.0.1:8900/healthz 2>/dev/null || echo "DOWN — start it: memnos serve"`

Recalled memories for "$ARGUMENTS": !`curl -s -m12 http://127.0.0.1:8900/recall -H "Authorization: Bearer mnk_..." -H 'Content-Type: application/json' -d "{\"namespace\":\"user:you\",\"query\":\"$ARGUMENTS\"}" 2>/dev/null | python3 -c "import sys,json; d=sys.stdin.read(); print((json.loads(d).get('context') if d.strip().startswith('{') else '') or '(no matching memories)')"`

Instructions:
- If a query was provided in "$ARGUMENTS", use the recalled memories above (no LLM at query
  time) to answer it.
- If no query was given, report the server health and remind me that memnos already
  auto-recalls before every prompt and auto-saves after each turn (via hooks), and that I
  can run `/memnos <query>` to search explicitly or use the `recall` / `remember` MCP tools.
```

## Set a folder's namespace (no restart)

`/memnos ns=proj:zudioz` pins this folder to a namespace (written to `~/.memnos/ns_overrides.json`, keyed by git-root; read live by the recall/remember hooks via `memnos_ns.resolve`). `/memnos ns` shows it; `/memnos ns list` lists granted + existing namespaces; `/memnos ns=clear` reverts. The hook token must be granted that namespace (a `proj:*` wildcard grant covers the per-project pattern).
