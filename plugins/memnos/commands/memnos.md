---
description: memnos memory — /memnos <query> recall · constraint <rule> · remember <fact> · ns=… · ? cheat sheet
allowed-tools: Bash(memnos:*)
---

!`A="$ARGUMENTS"; case "$A" in "?"|help|cheat|cheatsheet) : ;; constraint\ *|rule\ *|!*) R="${A#constraint }"; R="${R#rule }"; R="${R#!}"; memnos remember "$R" --type constraint --namespace auto;; remember\ *) memnos remember "${A#remember }" --namespace auto;; ns=*) memnos ns "${A#ns=}";; "ns clear") memnos ns clear;; "ns list"|list|ls) memnos namespace ls;; "ns prune") memnos namespace prune --empty --dry-run;; ""|ns) memnos ns;; *) memnos recall "$A" --namespace auto;; esac`

Instructions:
- If $ARGUMENTS is `?`, `help`, `cheat`, or `cheatsheet`: reply with EXACTLY the block below as
  your message text — do not call a tool, do not add commentary. (Bash stdout is invisible in
  compact/mobile UIs, so the cheat sheet is rendered by you, the model, instead.)

  ── MEMNOS CHEAT SHEET ──────────────────────────────
  /memnos <query>            recall memory for <query> (this folder's namespace)
  /memnos constraint <rule>  save a RULE — pinned into EVERY session for this project
  /memnos !<rule>            shorthand for constraint
  /memnos remember <fact>    save a durable fact/decision (normal memory)
  /memnos ns=proj:x          pin this folder's namespace
  /memnos ns                 show current namespace
  /memnos ns list            list namespaces
  /memnos ns clear           revert namespace pin
  /memnos ns prune           dry-run: show empty namespaces that could be cleaned up
  /memnos ?                  this cheat sheet

  TYPES: constraint(pinned) · decision · incident · skill · fact
  RULE OF THUMB: governs behavior → constraint · describes the world → remember
  CLI: memnos recall|remember [--type T]|ns|bindings|stats|status|constraint

  ADMIN CONSOLE (manage tokens): http://127.0.0.1:8900/admin
  LOGIN TO CONSOLE: mint an admin token → memnos token mint admin --label console
     (needs a '*' grant; token shown ONCE — paste into /admin. Grant if needed: memnos grant add <principal> '*')
  CONFIG (set admin_token here): ~/.memnos/config.json   ·   LOGS: ~/.memnos/server.log
  ─────────────────────────────────────────────────────

- `/memnos constraint <rule>` (or `/memnos !<rule>`) saves a **pinned constraint** at this folder's namespace — it is injected into EVERY future session for this project, so you never repeat it. Use for rules/guardrails: "never X", "always Y", "don't Z without my permission".
- `/memnos remember <fact>` saves a normal durable memory. `/memnos ns=…` manages the folder namespace.
- `/memnos <anything else>` recalls memory — use the recalled memories shown above to answer: $ARGUMENTS
