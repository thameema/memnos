#!/bin/sh
# SessionStart: say so plainly if the tommy CLI is missing (its MCP tools would fail to start).
command -v tommy >/dev/null 2>&1 || echo "tommy plugin: the tommy CLI is not on PATH, so Tommy's MCP tools are unavailable. Install it with: uv tool install tommy-orchestrator, then restart Claude Code."
exit 0
