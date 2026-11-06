"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.mongo import close_client, init_mongo
from app.db.postgres import SessionLocal, engine
from app.errors import register_exception_handlers
from app.logging_config import RequestIdMiddleware, configure_logging
from app.routes import (
    anomalies,
    assistant,
    auth,
    forecasts,
    health,
    internal,
    inventory,
    metrics,
    ml_admin,
    recommendations,
    sources,
)
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services.auth_service import seed_admin_if_empty
from app.services.ml_state import state

log = logging.getLogger(__name__)

DESCRIPTION = """\
Hybrid Virtualization Ops Platform.

Inventory from vSphere and Nutanix, metrics in MongoDB, anomaly detection,
30-day capacity forecasting and right-sizing recommendations, plus a read-only
assistant backed by a local model.

Authenticate with `POST /auth/login` and send `Authorization: Bearer <token>`.
Roles are ordered admin > operator > viewer.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    await init_mongo()

    async with SessionLocal() as session:
        await seed_admin_if_empty(session)

    # Artifacts may not exist yet on a cold start; reload() tolerates that.
    state.reload()

    if settings.enable_scheduler:
        start_scheduler()

    log.info("hvops api started")
    try:
        yield
    finally:
        shutdown_scheduler()
        await close_client()
        await engine.dispose()
        log.info("hvops api stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="HVOps API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)

    for module in (
        health,
        auth,
        sources,
        inventory,
        metrics,
        anomalies,
        forecasts,
        recommendations,
        assistant,
        ml_admin,
        internal,
    ):
        app.include_router(module.router)

    return app


app = create_app()
