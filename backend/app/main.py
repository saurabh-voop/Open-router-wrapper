"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

import app.services.files as file_service
from app.api import admin, auth, connectors, conversations, files, health, messages
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.llm import catalog
from app.llm.deps import get_openrouter
from app.router.config import get_routing_config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    config = get_routing_config()
    tasks: list[asyncio.Task[object]] = []
    try:
        await file_service.ensure_bucket()
    except Exception:  # dev convenience only; uploads will report the real error
        logger.warning("could not ensure the development S3 bucket exists")
    if settings.catalog_sync_enabled:
        client = get_openrouter()
        maker = get_sessionmaker()
        if settings.routing_strict or settings.is_production:
            report = await catalog.run_sync_once(maker, client, config)
            problems = ([report.error] if report.error else []) + report.warnings
            if problems:
                raise RuntimeError("routing.yaml is not usable: " + "; ".join(problems))
        else:
            tasks.append(asyncio.create_task(catalog.run_sync_once(maker, client, config)))
        tasks.append(asyncio.create_task(catalog.sync_forever(maker, client, config)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(title="Relay API", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(messages.router)
    app.include_router(files.router)
    app.include_router(connectors.router)
    app.include_router(admin.router)
    return app


app = create_app()
