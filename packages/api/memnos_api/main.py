"""
memnos_api.main — FastAPI application entry-point.

Start-up sequence
-----------------
1. Load MemnosConfig from MEMNOS_CONFIG env var (defaults to memnos.yaml)
2. Initialise MemnosClient + Orchestrator; store on app.state
3. Start MCP SSE server on port 8765 as a background task
4. Optionally start Telegram gateway (if config.gateway.telegram.enabled)
5. Optionally start WhatsApp gateway (if config.gateway.whatsapp.enabled)
6. Optionally start the learning scheduler (if config.learning.enabled)
7. Register all routers under /api/v1
8. On shutdown: close all connections cleanly

Entry point: ``memnos-server`` → ``memnos_api.main:main``
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background helpers
# ---------------------------------------------------------------------------

async def _start_mcp_server(config, client, orchestrator) -> None:
    """Start the MCP SSE server in the current event loop, sharing the existing client."""
    try:
        from memnos_mcp.transports.sse import run_sse_server  # type: ignore

        host = getattr(config.server, "host", "0.0.0.0")
        port = getattr(config.server, "mcp_port", 8765)
        await run_sse_server(
            config_path=None,
            host=host,
            port=port,
            shared_config=config,
            shared_client=client,
            shared_orchestrator=orchestrator,
        )
    except ImportError:
        logger.warning("memnos_mcp not installed; MCP SSE server not started")
    except Exception as exc:
        logger.exception("MCP SSE server crashed: %s", exc)


async def _start_telegram(config, client, orchestrator) -> None:
    """Start the Telegram bot in the current event loop."""
    try:
        from memnos_gateway.telegram.bot import TelegramGateway  # type: ignore

        tg_cfg = config.gateway.telegram  # type: ignore[attr-defined]
        # Merge allowed_users from YAML with TELEGRAM_ALLOWED_USERS env var (comma-separated)
        allowed_from_env = [
            int(u.strip()) for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
            if u.strip().isdigit()
        ]
        allowed_users = list({*(tg_cfg.allowed_users or []), *allowed_from_env})
        gateway = TelegramGateway(
            token=tg_cfg.bot_token,
            allowed_users=allowed_users,
            orchestrator=orchestrator,
            client=client,
            default_namespace=tg_cfg.default_namespace,
        )
        await gateway.start()
        logger.info("Telegram gateway started")
    except ImportError:
        logger.warning("memnos_gateway not installed; Telegram gateway not started")
    except Exception as exc:
        logger.exception("Telegram gateway failed to start: %s", exc)


async def _start_whatsapp(config, client, orchestrator, app: FastAPI) -> None:
    """Mount the WhatsApp webhook router on the running FastAPI app."""
    try:
        from memnos_gateway.whatsapp.webhook import router as wa_router  # type: ignore

        app.include_router(wa_router)
        logger.info("WhatsApp webhook router mounted at /webhook/whatsapp")
    except ImportError:
        logger.warning("memnos_gateway not installed; WhatsApp gateway not started")
    except Exception as exc:
        logger.exception("WhatsApp gateway failed to start: %s", exc)


async def _start_learning_scheduler(config, client) -> None:
    """Start the APScheduler-based learning scheduler."""
    try:
        from memnos_learning.episode_store import EpisodeStore  # type: ignore
        from memnos_learning.heuristic_store import HeuristicStore  # type: ignore
        from memnos_learning.quality_store import QualityStore  # type: ignore
        from memnos_learning.reflection import ReflectionService  # type: ignore
        from memnos_learning.decay import HeuristicDecayService  # type: ignore
        from memnos_learning.scheduler import LearningScheduler  # type: ignore

        episode_store = EpisodeStore()
        await episode_store.init()

        heuristic_store = HeuristicStore()
        await heuristic_store.init()

        quality_store = QualityStore()
        await quality_store.init()

        learning_cfg = config.learning
        reflection_cfg = learning_cfg.reflection

        api_key = getattr(config.runtime.api, "api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        model = getattr(reflection_cfg, "model", "claude-haiku-4-5-20251001")
        namespace = getattr(config.namespaces, "default", "personal:default")

        reflection_service = ReflectionService(
            api_key=api_key,
            model=model,
            episode_store=episode_store,
            heuristic_store=heuristic_store,
            namespace=namespace,
            memnos_client=client,
        )

        decay_service = HeuristicDecayService(
            heuristic_store=heuristic_store,
            inactive_days=getattr(learning_cfg.heuristic_decay, "inactive_days_before_decay", 30),
            decay_rate=getattr(learning_cfg.heuristic_decay, "decay_rate", 0.9),
        )

        def _make_reflection_service(ns: str) -> ReflectionService:
            """Factory for per-namespace ReflectionService instances."""
            return ReflectionService(
                api_key=api_key,
                model=model,
                episode_store=episode_store,
                heuristic_store=heuristic_store,
                namespace=ns,
                memnos_client=client,
            )

        scheduler = LearningScheduler(
            config=config,
            reflection_service=reflection_service,
            decay_service=decay_service,
            namespace=namespace,
            episode_store=episode_store,
            reflection_factory=_make_reflection_service,
        )
        scheduler.start()
        logger.info("Learning scheduler started")
    except ImportError:
        logger.warning("memnos_learning not installed; learning scheduler not started")
    except Exception as exc:
        logger.exception("Learning scheduler failed to start: %s", exc)


# ---------------------------------------------------------------------------
# App factory / lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan context manager — startup then shutdown."""

    # --- Startup ---
    config_path = os.environ.get("MEMNOS_CONFIG", "memnos.yaml")
    logger.info("Loading memnos config from %s", config_path)

    from memnos.config import MemnosConfig  # type: ignore
    from memnos.client import MemnosClient  # type: ignore
    from memnos_orchestrator.orchestrator import Orchestrator  # type: ignore
    from memnos_orchestrator.task_store import TaskStore  # type: ignore

    config = MemnosConfig.from_yaml(config_path)

    # Apply log level from config
    log_level = getattr(config.server, "log_level", "INFO")
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))

    client = MemnosClient(config)
    await client.start()
    logger.info("MemnosClient started")

    from memnos.migrations.runner import run_pending  # noqa: PLC0415
    applied = await run_pending(client._arcadedb)
    if applied:
        logger.info("Applied %d migration(s): %s", len(applied), applied)
    else:
        logger.info("Schema up to date, no migrations needed")

    task_store = TaskStore()
    await task_store.init()

    orchestrator = Orchestrator(config, client, task_store)
    await orchestrator.start()
    logger.info("Orchestrator started")

    # Initialise the runtime key store (SQLite-backed)
    from memnos_api.key_store import RuntimeKeyStore  # noqa: PLC0415

    key_store = RuntimeKeyStore()
    await key_store.init()
    logger.info("RuntimeKeyStore initialised")

    # Store singletons on app.state for dependency injection
    app.state.config = config
    app.state.client = client
    app.state.orchestrator = orchestrator
    app.state.key_store = key_store

    # Background tasks (fire-and-forget; errors are logged, not propagated)
    background_tasks: list[asyncio.Task] = []

    # MCP SSE server (shares the same MemnosClient + Orchestrator as the REST API)
    background_tasks.append(
        asyncio.create_task(
            _start_mcp_server(config, client, orchestrator), name="mcp-sse-server"
        )
    )

    # Telegram gateway
    gateway_cfg = getattr(config, "gateway", None)
    if gateway_cfg is not None:
        tg_cfg = getattr(gateway_cfg, "telegram", None)
        if tg_cfg is not None and getattr(tg_cfg, "enabled", False):
            background_tasks.append(
                asyncio.create_task(
                    _start_telegram(config, client, orchestrator),
                    name="telegram-gateway",
                )
            )

        # WhatsApp gateway (mounts router inline — no separate task needed)
        wa_cfg = getattr(gateway_cfg, "whatsapp", None)
        if wa_cfg is not None and getattr(wa_cfg, "enabled", False):
            await _start_whatsapp(config, client, orchestrator, app)

    # Learning scheduler
    if getattr(config.learning, "enabled", False):
        background_tasks.append(
            asyncio.create_task(
                _start_learning_scheduler(config, client),
                name="learning-scheduler",
            )
        )

    app.state.background_tasks = background_tasks

    yield  # ← application runs here

    # --- Shutdown ---
    logger.info("Shutting down memnos API server…")

    # Cancel all background tasks
    for task in background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # Close orchestrator and client (methods may not exist on all versions)
    stop_orchestrator = getattr(orchestrator, "stop", None)
    if stop_orchestrator is not None:
        try:
            await stop_orchestrator()
        except Exception as exc:
            logger.warning("Orchestrator stop error: %s", exc)

    stop_client = getattr(client, "stop", None)
    if stop_client is not None:
        try:
            await stop_client()
        except Exception as exc:
            logger.warning("MemnosClient stop error: %s", exc)

    try:
        await task_store.close()
    except Exception as exc:
        logger.warning("TaskStore close error: %s", exc)

    logger.info("memnos API server shutdown complete")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    import pathlib

    from fastapi.responses import HTMLResponse

    from memnos_api.routers import admin, episodes, graph, ingest, knowledge, memory, tasks, vault, viz  # noqa: PLC0415
    from memnos_api.routers.subscriptions import router as subscriptions_router  # noqa: PLC0415
    from memnos_api.routers.webhooks import router as webhooks_router  # noqa: PLC0415
    from memnos_api.routers.learning_admin import router as learning_router  # noqa: PLC0415
    from memnos_api.routers.agents import router as agents_router  # noqa: PLC0415
    from memnos_api.routers.corpus import router as corpus_router  # noqa: PLC0415
    from memnos_api.routers.extract import router as extract_router  # noqa: PLC0415
    from memnos_api.routers.consolidation import router as consolidation_router  # noqa: PLC0415
    from memnos_api.routers.context import router as context_router  # noqa: PLC0415

    application = FastAPI(
        title="memnos",
        description="Persistent memory and multi-agent orchestration layer",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — restrict in production via config; wide-open for dev
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers under /api/v1
    api_prefix = "/api/v1"
    application.include_router(memory.router, prefix=api_prefix)
    application.include_router(episodes.router, prefix=api_prefix)  # Feature 2 — immutable raw input layer
    application.include_router(ingest.router, prefix=api_prefix)    # Feature 4 — file ingestion (PDF/DOCX/MD/TXT)
    application.include_router(graph.router, prefix=api_prefix)
    application.include_router(tasks.router, prefix=api_prefix)
    application.include_router(admin.router, prefix=api_prefix)
    application.include_router(vault.router, prefix=api_prefix)
    application.include_router(viz.router, prefix=api_prefix)
    application.include_router(knowledge.router, prefix=api_prefix)
    application.include_router(subscriptions_router, prefix=api_prefix)
    application.include_router(webhooks_router, prefix=api_prefix)
    application.include_router(learning_router, prefix=api_prefix)
    application.include_router(agents_router, prefix=api_prefix)
    application.include_router(corpus_router, prefix=api_prefix)
    application.include_router(extract_router, prefix=api_prefix)       # auto-extraction from raw text
    application.include_router(consolidation_router, prefix=api_prefix)  # two-layer consolidation
    application.include_router(context_router, prefix=api_prefix)        # context injection for LLM prompts

    # Interactive knowledge graph dashboard
    _dashboard_path = pathlib.Path(__file__).parent / "static" / "dashboard.html"

    @application.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
    async def dashboard(request: Request):
        """
        Serve the interactive knowledge graph dashboard.

        The server injects its own API key and base URL directly into the page
        so the user never needs to enter credentials manually.  The settings
        modal is still available for overriding when memnos is accessed remotely
        with a different key.
        """
        try:
            html = _dashboard_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)

        # Resolve the first configured API key from the running server config.
        api_key = ""
        config = getattr(request.app.state, "config", None)
        if config is not None:
            auth_cfg = getattr(config, "auth", None)
            keys = getattr(auth_cfg, "api_keys", []) if auth_cfg else []
            if keys:
                first = keys[0]
                api_key = getattr(first, "key", "") if hasattr(first, "key") else str(first)

        # Build the base URL as the browser sees it (scheme + host).
        base_url = str(request.base_url).rstrip("/")

        # Inject an auto-config block that pre-populates sessionStorage so the
        # settings modal never appears when opening the dashboard locally.
        # The user can still override via the gear icon.
        inject = (
            "<script>\n"
            "/* Auto-injected by memnos server — no manual API key needed */\n"
            f"sessionStorage.setItem('memnos_key', {repr(api_key)});\n"
            f"sessionStorage.setItem('memnos_url', {repr(base_url)});\n"
            "</script>\n"
        )
        html = html.replace("</head>", inject + "</head>", 1)
        return HTMLResponse(content=html)

    @application.get("/", include_in_schema=False)
    async def root():
        return {"service": "memnos", "version": "0.1.0", "docs": "/docs", "dashboard": "/dashboard"}

    return application


app = create_app()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the memnos REST API server via uvicorn."""
    import uvicorn
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config_path = os.environ.get("MEMNOS_CONFIG", "memnos.yaml")

    # Quick config read for host/port before the lifespan boots
    try:
        from memnos.config import MemnosConfig  # type: ignore

        cfg = MemnosConfig.from_yaml(config_path)
        host = cfg.server.host
        port = cfg.server.api_port
        log_level = cfg.server.log_level.lower()
    except Exception:
        host = "0.0.0.0"
        port = 8766
        log_level = "info"

    uvicorn.run(
        "memnos_api.main:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
