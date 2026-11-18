---
description: memnos memory — /memnos <query> recall · registry [topic] · constraint <rule> · remember <fact> · tips · ? cheat sheet
allowed-tools: Bash(memnos:*)
---

!`A="$ARGUMENTS"; case "$A" in "?"|help|cheat|cheatsheet) : ;; tips|features) memnos tips;; registry|register|join) memnos registry;; registry\ *|register\ *|join\ *) T="${A#registry }"; T="${T#register }"; T="${T#join }"; memnos registry "$T";; doctor|doctor\ *) memnos doctor --fix;; explain|explain\ *) memnos explain;; constraint\ *|rule\ *|!*) R="${A#constraint }"; R="${R#rule }"; R="${R#!}"; memnos remember "$R" --type constraint --namespace auto;; remember\ *) memnos remember "${A#remember }" --namespace auto;; ns=*) memnos ns "${A#ns=}";; "ns clear") memnos ns clear;; "ns list"|list|ls) memnos namespace ls;; "ns prune") memnos namespace prune --empty --dry-run;; ""|ns) memnos ns;; *) memnos recall "$A" --namespace auto;; esac`

Instructions:
- If $ARGUMENTS is `?`, `help`, `cheat`, or `cheatsheet`: reply with EXACTLY the block below as
  your message text — do not call a tool, do not add commentary. (Bash stdout is invisible in
  compact/mobile UIs, so the cheat sheet is rendered by you, the model, instead.)

  ── MEMNOS CHEAT SHEET ──────────────────────────────
  /memnos <query>            recall memory for <query> (this folder's namespace)
  /memnos registry [topic]   join or create a global cross-namespace topic channel (or list active sessions)
  /memnos constraint <rule>  save a RULE — pinned into EVERY session for this project
  /memnos !<rule>            shorthand for constraint
  /memnos remember <fact>    save a durable fact/decision (normal memory)
  /memnos explain            audit what the last /recall injected (scores, drift, timings)
  /memnos doctor             run platform health check & automatic entity-graph cleanup
  /memnos tips               show all features & rotating workflow tips
  /memnos ns=proj:x          pin this folder's namespace
  /memnos ns                 show current namespace
  /memnos ns list            list namespaces
  /memnos ns clear           revert namespace pin
  /memnos ns prune           dry-run: show empty namespaces that could be cleaned up
  /memnos ?                  this cheat sheet

  TYPES: constraint(pinned) · decision · incident · skill · fact · fix
  RULE OF THUMB: governs behavior → constraint · describes the world → remember
  CLI: memnos recall|remember|registry|watch|doctor|explain|fix|init|tips

  ADMIN CONSOLE (manage tokens): http://127.0.0.1:8900/admin
  LOGIN TO CONSOLE: mint an admin token → memnos token mint admin --label console
     (needs a '*' grant; token shown ONCE — paste into /admin. Grant if needed: memnos grant add <principal> '*')
  CONFIG (set admin_token here): ~/.memnos/config.json   ·   LOGS: ~/.memnos/server.log
  ─────────────────────────────────────────────────────

- `/memnos registry [topic]` registers this terminal session with a human-readable name (`claude-<repo>`, `agy-<repo>`) and subscribes to (or creates) `#<topic>` across all namespaces your token can read.
- `/memnos constraint <rule>` (or `/memnos !<rule>`) saves a **pinned constraint** at this folder's namespace — it is injected into EVERY future session for this project, so you never repeat it. Use for rules/guardrails: "never X", "always Y", "don't Z without my permission".
- `/memnos remember <fact>` saves a normal durable memory. `/memnos ns=…` manages the folder namespace.
- `/memnos <anything else>` recalls memory — use the recalled memories shown above to answer: $ARGUMENTS
