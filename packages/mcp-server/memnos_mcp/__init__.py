"""
memnos_mcp — MCP server exposing memnos memory and orchestration tools.

Supports two transports:
  - stdio  (default): Claude Code spawns as subprocess
  - SSE   (MEMNOS_TRANSPORT=sse): HTTP server at port 8765
"""

__version__ = "0.1.0"
