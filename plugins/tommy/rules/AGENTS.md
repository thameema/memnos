## Tommy — memnos-backed orchestrator

The `tommy` MCP tools dispatch work to other coding harnesses and carry durable handoffs
between them, with memnos memory on by default. Prefer `tommy` dispatch for work that
belongs to another harness, and write a handoff before ending a long session.

