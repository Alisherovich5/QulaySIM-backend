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


SITEMAP_STATIC_PATHS = (
    ("/", "1.0", "daily"),
    ("/destinations", "0.9", "daily"),
    ("/device-check", "0.7", "monthly"),
    ("/support", "0.6", "monthly"),
)

# Uzbek is served from the root; the other two live under a path prefix.
# This must agree with src/lib/seo.ts on the front end — the sitemap and the
# hreflang tags on the pages themselves have to describe the same set of
# URLs, or Google discards the whole group as inconsistent.
SITEMAP_DEFAULT_LANG = "uz"
SITEMAP_LANGS = ("uz", "ru", "en")


def _localised_url(base: str, path: str, lang: str) -> str:
    """The address of one page in one language, canonical form."""
    prefix = "" if lang == SITEMAP_DEFAULT_LANG else f"/{lang}"
    if path == "/":
        return f"{base}{prefix}" if prefix else f"{base}/"
    return f"{base}{prefix}{path}"


def _sitemap_entries(
    base: str, path: str, priority: str, freq: str, lastmod: str | None = None
) -> list[str]:
    """One <url> per language, each listing every language including itself.

    Reciprocity is not optional: an edition that does not name itself among
    its own alternates makes the set invalid and Google drops all of it. So
    the same block of xhtml:link elements is repeated under each <loc>.
    """
    alternates = [
        f'<xhtml:link rel="alternate" hreflang="{lang}" '
        f'href="{_localised_url(base, path, lang)}"/>'
        for lang in SITEMAP_LANGS
    ]
    alternates.append(
        '<xhtml:link rel="alternate" hreflang="x-default" '
        f'href="{_localised_url(base, path, SITEMAP_DEFAULT_LANG)}"/>'
    )
    joined = "".join(alternates)
    # lastmod tells a crawler which of two hundred pages is worth re-reading.
    # Omitted rather than guessed when we do not know: an invented date is worse
    # than none, because Google stops trusting the field across the whole site.
    stamp = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    return [
        f"  <url><loc>{_localised_url(base, path, lang)}</loc>{stamp}"
        f"<changefreq>{freq}</changefreq><priority>{priority}</priority>"
        f"{joined}</url>"
        for lang in SITEMAP_LANGS
    ]


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

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap() -> Response:
        async def produce() -> str:
            from sqlalchemy import func, select

            from app.core.config import settings as cfg
            from app.db.models import Country, Plan, SupplierOffer

            base = (cfg.public_base_url or "https://qulaysim.uz").rstrip("/")
            lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
                ' xmlns:xhtml="http://www.w3.org/1999/xhtml">',
            ]
            for path, priority, freq in SITEMAP_STATIC_PATHS:
                lines.extend(_sitemap_entries(base, path, priority, freq))
            # A destination page changes when its offers do — that is where the
            # prices on it come from — so the newest supplier-offer timestamp for
            # the country is its real last-modified date. Country itself carries
            # no timestamp to use.
            async with SessionFactory() as session:
                rows = (
                    await session.execute(
                        select(Country.slug, func.max(SupplierOffer.updated_at))
                        .outerjoin(Plan, Plan.country_id == Country.id)
                        .outerjoin(SupplierOffer, SupplierOffer.plan_id == Plan.id)
                        .where(Country.is_active.is_(True))
                        .group_by(Country.slug)
                        .order_by(Country.slug)
                    )
                ).all()
            for slug, updated in rows:
                lines.extend(
                    _sitemap_entries(
                        base,
                        f"/destinations/{slug}",
                        "0.8",
                        "weekly",
                        updated.date().isoformat() if updated else None,
                    )
                )
            lines.append("</urlset>")
            return "\n".join(lines)

        body = await get_or_set(cache_key("sitemap"), 86400, produce)
        return Response(content=body, media_type="application/xml")


app = create_app()
