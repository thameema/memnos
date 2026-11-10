## memnos — long-term memory (MCP tools)

You have persistent, namespace-scoped memory through the `memnos` MCP tools. Memory is
recalled automatically before each prompt and saved automatically after each turn.
- Use `recall` (and `recall_wide` across namespaces) instead of ad-hoc file search for past decisions.
- Use `remember` to store durable facts: decisions, ticket IDs, outcomes.
- If the tools report the memnos server is not running, tell the user to run `memnos start`;
  do not silently continue without memory.

