"""
memnos_mcp.transports.sse — SSE transport for the memnos MCP server.

Exposes the MCP server over HTTP via Server-Sent Events so that
Claude Code (and other MCP clients) can connect over the network.

Default bind: http://0.0.0.0:8765

Routes
------
GET  /health    — simple liveness probe (no auth required)
GET  /sse       — SSE endpoint; MCP client connects here
POST /messages  — MCP message endpoint (JSON-RPC over HTTP)

The APIKeyMiddleware from memnos_mcp.auth is installed when
``config.auth.api_keys`` is non-empty.

Usage
-----
  MEMNOS_TRANSPORT=sse memnos-mcp
  # or:
  python -m memnos_mcp.transports.sse
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

# Namespace used for auto-created session Episodes.
# Override with MEMNOS_SESSION_NAMESPACE env var.
_SESSION_NS = os.environ.get("MEMNOS_SESSION_NAMESPACE", "default")


async def _open_session_episode(client, request: Request) -> str | None:
    """Create an Episode when an MCP client connects. Returns the episode id or None."""
    try:
        ua = request.headers.get("user-agent", "")
        tool = "claude-code" if "claude" in ua.lower() else "cursor" if "cursor" in ua.lower() else "mcp-client"
        ep = await client.create_episode(
            namespace=_SESSION_NS,
            title=f"MCP Session — {tool}",
            tags=["mcp-session", tool],
        )
        logger.info("Session episode opened: %s (%s)", ep.id, tool)
        return ep.id
    except Exception as exc:
        logger.debug("Could not open session episode: %s", exc)
        return None


async def _close_session_episode(client, episode_id: str | None) -> None:
    """Close the session Episode when the MCP client disconnects."""
    if not episode_id:
        return
    try:
        await client.close_episode(episode_id)
        logger.info("Session episode closed: %s", episode_id)
    except Exception as exc:
        logger.debug("Could not close session episode %s: %s", episode_id, exc)

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(client, orchestrator, config) -> FastAPI:  # noqa: C901
    """
    Build the FastAPI application that wraps the MCP server over SSE.

    The app uses the ``mcp`` SDK's SseServerTransport when available,
    otherwise falls back to a manual SSE implementation using
    ``sse_starlette``.
    """
    from memnos_mcp.server import create_mcp_server

    mcp_server = create_mcp_server(client, orchestrator, config)

    # Try the official MCP SSE transport first
    try:
        from mcp.server.sse import SseServerTransport  # type: ignore

        _setup_with_sdk_sse(mcp_server, config)
        sse_transport = SseServerTransport("/messages")

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            logger.info("memnos MCP SSE server starting (SDK SseServerTransport)")
            yield
            logger.info("memnos MCP SSE server stopping")

        app = FastAPI(title="memnos MCP Server", version="0.1.0", lifespan=lifespan)
        _install_auth_middleware(app, config)
        _add_health_route(app)
        _add_sdk_sse_routes(app, mcp_server, sse_transport, client=client)

    except ImportError:
        logger.warning(
            "mcp.server.sse.SseServerTransport not available; "
            "falling back to manual SSE via sse_starlette"
        )

        @asynccontextmanager
        async def lifespan(app: FastAPI):  # type: ignore[misc]
            logger.info("memnos MCP SSE server starting (manual SSE)")
            yield
            logger.info("memnos MCP SSE server stopping")

        app = FastAPI(title="memnos MCP Server", version="0.1.0", lifespan=lifespan)
        _install_auth_middleware(app, config)
        _add_health_route(app)
        _add_manual_sse_routes(app, mcp_server, client=client)

    return app


def _setup_with_sdk_sse(mcp_server, config) -> None:
    """No-op placeholder; reserved for SDK-level configuration."""


def _install_auth_middleware(app: FastAPI, config) -> None:
    """Attach APIKeyMiddleware if the config has api_keys defined."""
    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    if api_keys:
        from memnos_mcp.auth import APIKeyMiddleware

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
        return JSONResponse({"status": "ok", "service": "memnos-mcp"})


def _add_sdk_sse_routes(app: FastAPI, mcp_server, sse_transport, client=None) -> None:
    """Register /sse and /messages routes using the MCP SDK's transport."""

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        """SSE endpoint — MCP client opens a persistent connection here."""
        episode_id = await _open_session_episode(client, request) if client else None
        try:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send  # type: ignore[attr-defined]
            ) as streams:
                read_stream, write_stream = streams
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
        finally:
            if client:
                await _close_session_episode(client, episode_id)

    @app.post("/messages")
    async def messages_endpoint(request: Request) -> Response:
        """JSON-RPC message endpoint for the SSE transport."""
        return await sse_transport.handle_post_message(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )


def _add_manual_sse_routes(app: FastAPI, mcp_server, client=None) -> None:
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

        episode_id = await _open_session_episode(client, request) if client else None

        async def event_generator():
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
                if client:
                    await _close_session_episode(client, episode_id)

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
    shared_config=None,
    shared_client=None,
    shared_orchestrator=None,
) -> None:
    """
    Load config, start services, and serve over SSE/HTTP.

    Parameters
    ----------
    config_path          : path to memnos YAML config (ignored if shared_config given)
    host                 : bind host (default: config.server.host or 0.0.0.0)
    port                 : bind port (default: config.server.mcp_port or 8765)
    shared_config        : pre-loaded MemnosConfig (avoids double-init when called from REST API)
    shared_client        : pre-started MemnosClient  (shared with REST API)
    shared_orchestrator  : pre-started Orchestrator  (shared with REST API)
    """
    from memnos_mcp.server import _load_config, _start_services

    if shared_config is not None:
        config = shared_config
        client = shared_client
        orchestrator = shared_orchestrator
        logger.info("memnos MCP (SSE) — using shared config from REST API process")
    else:
        resolved_path = config_path or os.environ.get("MEMNOS_CONFIG", "memnos.yaml")
        logger.info("memnos MCP (SSE) — loading config from %s", resolved_path)
        config = _load_config(resolved_path)
        client, orchestrator = await _start_services(config)

    bind_host = host or getattr(getattr(config, "server", None), "host", _DEFAULT_HOST) or _DEFAULT_HOST
    bind_port = port or int(getattr(getattr(config, "server", None), "mcp_port", _DEFAULT_PORT) or _DEFAULT_PORT)

    app = create_app(client, orchestrator, config)

    logger.info("Starting memnos MCP SSE server on %s:%d", bind_host, bind_port)

    uv_config = uvicorn.Config(
        app=app,
        host=bind_host,
        port=bind_port,
        log_level=os.environ.get("MEMNOS_LOG_LEVEL", "info").lower(),
        access_log=True,
    )
    server = uvicorn.Server(uv_config)
    await server.serve()


def main() -> None:
    """Standalone entry point for running the SSE server directly."""
    import logging as _logging

    log_level = os.environ.get("MEMNOS_LOG_LEVEL", "INFO").upper()
    _logging.basicConfig(
        level=getattr(_logging, log_level, _logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    asyncio.run(run_sse_server())


if __name__ == "__main__":
    main()
