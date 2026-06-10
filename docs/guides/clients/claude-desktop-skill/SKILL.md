---
name: memnos-memory
description: >-
  Long-term memory via memnos. Use when the user references past work, prior decisions,
  preferences, people, projects, or anything from earlier conversations — and after giving
  answers that contain decisions, identifiers, or outcomes worth keeping.
---

# memnos — long-term memory

You have persistent, governed long-term memory through the **memnos** MCP tools
(`recall`, `recall_wide`, `remember`, `reconcile_claim`, `get_entity`, `get_provenance`).
Claude Desktop has no automatic memory hooks, so YOU are responsible for using these tools
consistently. Follow these rules:

## Recall — before answering
- When the user references past conversations, prior decisions, preferences, ongoing
  projects, or people/things not introduced in this session, call `recall` with a focused
  query **before** answering. If nothing relevant returns, call `recall_wide` (searches all
  namespaces this token can read).
- Prefer recalled facts over guessing. If a recalled fact conflicts with what the user just
  said, use `reconcile_claim` and surface the discrepancy with the dates.

## Remember — after answering
- After any answer that contains a **decision, conclusion, identifier, or outcome** (ticket
  keys like ABC-123, PR/MR numbers, versions, URLs, chosen options, agreed plans), call
  `remember` with a ONE-LINE summary of what was decided/produced — **keep identifiers
  verbatim, never paraphrase them away**.
- Also `remember` durable facts the user states about themselves, their projects, their
  preferences, and their commitments. One fact per call, self-contained, with dates
  resolved to absolute form.
- Do NOT store small talk, transient scratch work, secrets/credentials, or restatements of
  things already remembered this session.

## Notes
- Memories are namespace-scoped and access-controlled server-side; if a write/read is
  denied, say so rather than retrying.
- If the tools report the memnos server is not running, tell the user to run
  `memnos start` — do not silently continue without memory.
