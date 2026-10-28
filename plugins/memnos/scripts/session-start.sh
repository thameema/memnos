#!/bin/sh
# SessionStart: say so plainly if the memnos CLI is missing, else show the memory status line.
if ! command -v memnos >/dev/null 2>&1; then
  echo "memnos plugin: the memnos CLI is not on PATH, so memory is OFF. Install it with: uv tool install memnos && memnos setup  (embedded Postgres, no Docker), then restart Claude Code."
  exit 0
fi
exec memnos hook status
