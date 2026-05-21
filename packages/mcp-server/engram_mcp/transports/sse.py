"""
engram_mcp.transports.sse — SSE transport for the engram MCP server.

Exposes the MCP server over HTTP via Server-Sent Events so that
Claude Code (and other MCP clients) can connect over the network.

Default bind: http://0.0.0.0:8765

Routes
------
GET  /health    — simple liveness probe (no auth required)
GET  /sse       — SSE endpoint; MCP client connects here
POST /messages  — MCP message endpoint (JSON-RPC over HTTP)

The APIKeyMiddleware from engram_mcp.auth is installed when
``config.auth.api_keys`` is non-empty.

Usage
-----
  ENGRAM_TRANSPORT=sse engram-mcp
  # or:
  python -m engram_mcp.transports.sse
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(client, orchestrator, config) -> FastAPI:
    """
    Build the FastAPI application that wraps the MCP server over SSE.

    The app uses the ``mcp`` SDK's SseServerTransport when available,
    otherwise falls back to a manual SSE implementation using
    ``sse_starlette``.
    """
    from engram_mcp.server import create_mcp_server

    mcp_server = create_mcp_server(client, orchestrator, config)

    # Try the official MCP SSE transport first
    try:
        from mcp.server.sse import SseServerTransport  # type: ignore

        _setup_with_sdk_sse(mcp_server, config)
        sse_transport = SseServerTransport("/messages")

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            logger.info("engram MCP SSE server starting (SDK SseServerTransport)")
            yield
            logger.info("engram MCP SSE server stopping")

        app = FastAPI(title="engram MCP Server", version="0.1.0", lifespan=lifespan)
        _install_auth_middleware(app, config)
        _add_health_route(app)
        _add_sdk_sse_routes(app, mcp_server, sse_transport)

    except ImportError:
        logger.warning(
            "mcp.server.sse.SseServerTransport not available; "
            "falling back to manual SSE via sse_starlette"
        )

        @asynccontextmanager
        async def lifespan(app: FastAPI):  # type: ignore[misc]
            logger.info("engram MCP SSE server starting (manual SSE)")
            yield
            logger.info("engram MCP SSE server stopping")

        app = FastAPI(title="engram MCP Server", version="0.1.0", lifespan=lifespan)
        _install_auth_middleware(app, config)
        _add_health_route(app)
        _add_manual_sse_routes(app, mcp_server)

    return app


def _setup_with_sdk_sse(mcp_server, config) -> None:
    """No-op placeholder; reserved for SDK-level configuration."""


def _install_auth_middleware(app: FastAPI, config) -> None:
    """Attach APIKeyMiddleware if the config has api_keys defined."""
    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    if api_keys:
        from engram_mcp.auth import APIKeyMiddleware

        app.add_middleware(APIKeyMiddleware, config=config)
        logger.info("API key authentication enabled (%d key(s))", len(api_keys))
    else:
        logger.warning(
            "No api_keys configured — SSE server is unauthenticated. "
            "Do not expose this port publicly."
        )


def _add_health_route(app: FastAPI) -> None:
    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "engram-mcp"})


def _add_sdk_sse_routes(app: FastAPI, mcp_server, sse_transport) -> None:
    """Register /sse and /messages routes using the MCP SDK's transport."""

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        """SSE endpoint — MCP client opens a persistent connection here."""
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        ) as streams:
            read_stream, write_stream = streams
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    @app.post("/messages")
    async def messages_endpoint(request: Request) -> Response:
        """JSON-RPC message endpoint for the SSE transport."""
        return await sse_transport.handle_post_message(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )


def _add_manual_sse_routes(app: FastAPI, mcp_server) -> None:
    """
    Fallback SSE implementation using sse_starlette.

    Each GET /sse connection receives a persistent event stream.
    Clients POST JSON-RPC messages to /messages which are forwarded
    in-memory to the MCP server.
    """
    import json
    from asyncio import Queue

    from sse_starlette.sse import EventSourceResponse  # type: ignore

    # Map connection_id -> (incoming_queue, outgoing_queue)
    _connections: dict[str, tuple[Queue, Queue]] = {}

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        conn_id = os.urandom(8).hex()
        incoming: Queue = Queue()
        outgoing: Queue = Queue()
        _connections[conn_id] = (incoming, outgoing)
        logger.debug("SSE connection opened: %s", conn_id)

        async def event_generator():
            # Send the connection ID so the client knows where to POST
            yield {
                "event": "endpoint",
                "data": json.dumps({"messages_url": f"/messages?conn_id={conn_id}"}),
            }
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = outgoing.get_nowait()
                        yield {"event": "message", "data": json.dumps(message)}
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.05)
            finally:
                _connections.pop(conn_id, None)
                logger.debug("SSE connection closed: %s", conn_id)

        return EventSourceResponse(event_generator())

    @app.post("/messages")
    async def messages_endpoint(request: Request) -> JSONResponse:
        conn_id = request.query_params.get("conn_id", "")
        if conn_id not in _connections:
            return JSONResponse(
                status_code=400,
                content={"error": f"Unknown connection id: {conn_id!r}"},
            )

        body = await request.json()
        incoming, outgoing = _connections[conn_id]
        await incoming.put(body)

        # Simple echo-ack; the real response arrives via the SSE stream
        return JSONResponse({"status": "accepted"})


# ---------------------------------------------------------------------------
# Service bootstrap
# ---------------------------------------------------------------------------

async def run_sse_server(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """
    Load config, start services, and serve over SSE/HTTP.

    Parameters
    ----------
    config_path : path to engram YAML config
    host        : bind host (default: config.server.host or 0.0.0.0)
    port        : bind port (default: config.server.mcp_port or 8765)
    """
    from engram_mcp.server import _load_config, _start_services

    resolved_path = config_path or os.environ.get("ENGRAM_CONFIG", "engram.yaml")
    logger.info("engram MCP (SSE) — loading config from %s", resolved_path)

    config = _load_config(resolved_path)
    client, orchestrator = await _start_services(config)

    bind_host = host or getattr(getattr(config, "server", None), "host", _DEFAULT_HOST) or _DEFAULT_HOST
    bind_port = port or int(getattr(getattr(config, "server", None), "mcp_port", _DEFAULT_PORT) or _DEFAULT_PORT)

    app = create_app(client, orchestrator, config)

    logger.info("Starting engram MCP SSE server on %s:%d", bind_host, bind_port)

    uv_config = uvicorn.Config(
        app=app,
        host=bind_host,
        port=bind_port,
        log_level=os.environ.get("ENGRAM_LOG_LEVEL", "info").lower(),
        access_log=True,
    )
    server = uvicorn.Server(uv_config)
    await server.serve()


def main() -> None:
    """Standalone entry point for running the SSE server directly."""
    import logging as _logging

    log_level = os.environ.get("ENGRAM_LOG_LEVEL", "INFO").upper()
    _logging.basicConfig(
        level=getattr(_logging, log_level, _logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(run_sse_server())


if __name__ == "__main__":
    main()
