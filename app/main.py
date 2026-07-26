"""ASGI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.v1.routers import (
    account,
    auth,
    catalog,
    checkout,
    content,
    health,
    payme,
    support,
    webhooks,
)
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware

configure_logging(settings.log_level, json_output=settings.is_production)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info("app.startup", environment=settings.environment, version="2.0.0")
    yield
    # Close pools explicitly so in-flight connections drain on SIGTERM instead
    # of being severed mid-query during a rolling deploy.
    from app.core.cache import close_redis
    from app.db.session import dispose_engine
    from app.integrations.http import close_client

    await close_client()
    await close_redis()
    await dispose_engine()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="QulaySIM API",
        description="eSIM commerce API — storefront, checkout, account and provisioning.",
        version="2.0.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        # No interactive docs in production: they enumerate every endpoint and schema.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    allow_origins = settings.cors_origins_list
    # The permissive localhost regex is a development convenience only.
    origin_regex = None if settings.is_production else r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    register_middleware(app)
    register_exception_handlers(app)

    for router in (
        health.router,
        auth.router,
        catalog.router,
        content.router,
        checkout.router,
        account.router,
        payme.router,
        support.router,
        webhooks.router,
    ):
        app.include_router(router)

    return app


app = create_app()
