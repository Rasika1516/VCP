"""FastAPI application factory for the VCP Control Plane."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vcp_control_plane.api.deps import set_redis_client
from vcp_control_plane.api.routers import events, fleet, profiles, scenarios, vcps
from vcp_control_plane.bus.subscriber import EventSubscriber
from vcp_control_plane.config.settings import get_settings
from vcp_control_plane.db.base import dispose_engine

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _configure_logging(level: str, redis_event_log_file: str) -> None:
    """Attach handlers to the package logger (uvicorn only configures its own loggers)."""
    pkg_logger = logging.getLogger("vcp_control_plane")
    pkg_logger.setLevel(level.upper())
    if not pkg_logger.handlers:  # create_app may be called more than once (reload, tests)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_LOG_FORMAT))
        pkg_logger.addHandler(console)
        pkg_logger.propagate = False

    event_logger = logging.getLogger("vcp_control_plane.redis_events")
    if redis_event_log_file and not event_logger.handlers:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        Path(redis_event_log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            redis_event_log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        event_logger.addHandler(file_handler)  # still propagates to the console handler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    settings = get_settings()

    # --- Redis ---
    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        ssl=settings.redis_ssl,
        db=settings.redis_db,
        decode_responses=True,
    )
    set_redis_client(redis_client)
    logger.info("Redis client connected to %s:%d", settings.redis_host, settings.redis_port)

    # --- Event subscriber (background task: reads worker events → broadcasts to WS) ---
    subscriber = EventSubscriber(redis_client=redis_client, settings=settings)
    app.state.event_subscriber = subscriber
    import asyncio
    sub_task = asyncio.create_task(subscriber.run())

    logger.info("VCP Control Plane ready — listening on %s:%d", settings.api_host, settings.api_port)
    yield

    # --- Shutdown ---
    sub_task.cancel()
    try:
        await sub_task
    except asyncio.CancelledError:
        pass
    await redis_client.aclose()
    await dispose_engine()
    logger.info("VCP Control Plane shut down cleanly")


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.redis_event_log_file)

    app = FastAPI(
        title="VCP Fleet Simulator — Control & Management Plane",
        version="0.1.0",
        description=(
            "REST + WebSocket API for controlling and monitoring the VCP simulator fleet. "
            "Dispatches commands via Redis Streams to data-plane workers."
        ),
        lifespan=lifespan,
    )

    # CORS — allow the React dev server and any configured frontend origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url, "http://localhost:5173", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Routers ---
    app.include_router(vcps.router, prefix="/api/v1", tags=["VCPs"])
    app.include_router(fleet.router, prefix="/api/v1", tags=["Fleet"])
    app.include_router(profiles.router, prefix="/api/v1", tags=["Profiles"])
    app.include_router(scenarios.router, prefix="/api/v1", tags=["Scenarios"])
    app.include_router(events.router, prefix="/api/v1", tags=["Events"])

    @app.get("/healthz", tags=["Health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "vcp-control-plane"}

    return app
