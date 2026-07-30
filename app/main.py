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

    _register_sitemap(app)

    return app


def _register_sitemap(app: FastAPI) -> None:
    """Serve /sitemap.xml from the API rather than shipping a static file.

    The list of destination pages *is* the catalogue, so a file checked into the
    front end goes stale the first time a country is added or deactivated. The
    API already knows, and the answer is cached for a day.

    Registered at the root, not under /api, because that is where crawlers look;
    nginx proxies the single path across.
    """
    from fastapi import Response

    from app.core.cache import cache_key, get_or_set
    from app.db.session import SessionFactory

    STATIC_PATHS = (
        ("/", "1.0", "daily"),
        ("/destinations", "0.9", "daily"),
        ("/device-check", "0.7", "monthly"),
        ("/support", "0.6", "monthly"),
    )

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap() -> Response:
        async def produce() -> str:
            from sqlalchemy import select

            from app.core.config import settings as cfg
            from app.db.models import Country

            base = (cfg.public_base_url or "https://qulaysim.uz").rstrip("/")
            lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for path, priority, freq in STATIC_PATHS:
                lines.append(
                    f"  <url><loc>{base}{path}</loc>"
                    f"<changefreq>{freq}</changefreq><priority>{priority}</priority></url>"
                )
            async with SessionFactory() as session:
                slugs = (
                    await session.execute(
                        select(Country.slug)
                        .where(Country.is_active.is_(True))
                        .order_by(Country.slug)
                    )
                ).scalars().all()
            for slug in slugs:
                lines.append(
                    f"  <url><loc>{base}/destinations/{slug}</loc>"
                    f"<changefreq>weekly</changefreq><priority>0.8</priority></url>"
                )
            lines.append("</urlset>")
            return "\n".join(lines)

        body = await get_or_set(cache_key("sitemap"), 86400, produce)
        return Response(content=body, media_type="application/xml")


app = create_app()
