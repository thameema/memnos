"""
memnos_mcp.transports.stdio — stdio transport for the memnos MCP server.

Claude Code spawns this process as a subprocess and communicates via
stdin / stdout using the MCP wire protocol.

Usage
-----
  # via pyproject.toml entry-point:
  memnos-mcp-stdio

  # or directly:
  python -m memnos_mcp.transports.stdio
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)


async def run(config_path: str | None = None) -> None:
    """
    Load config, start services, and serve over stdio.

    Parameters
    ----------
    config_path : path to the memnos YAML config file.
                  Falls back to the MEMNOS_CONFIG env var, then "memnos.yaml".
    """
    from mcp.server.stdio import stdio_server  # type: ignore

    from memnos_mcp.server import _load_config, _start_services, create_mcp_server

    resolved_path = config_path or os.environ.get("MEMNOS_CONFIG", "memnos.yaml")

    logger.info("memnos MCP (stdio) — loading config from %s", resolved_path)
    config = _load_config(resolved_path)

    logger.info("Starting MemnosClient and Orchestrator…")
    client, orchestrator = await _start_services(config)

    server = create_mcp_server(client, orchestrator, config)
    init_options = server.create_initialization_options()

    logger.info("memnos MCP server ready (stdio transport)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Entry point for the ``memnos-mcp-stdio`` CLI command."""
    import logging as _logging

    log_level = os.environ.get("MEMNOS_LOG_LEVEL", "INFO").upper()
    _logging.basicConfig(
        level=getattr(_logging, log_level, _logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(run())


if __name__ == "__main__":
    main()
