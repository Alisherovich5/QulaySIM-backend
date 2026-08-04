"""Open Graph cards, one per destination.

Served under /api on purpose: the edge proxy hands /api/* straight to this
service and everything else to the storefront's nginx, so any other path would
mean touching two proxy configs for one image route. og:image consumers
(Telegram, Facebook, Twitter) fetch like ordinary clients and don't care what
the path looks like.
"""

from __future__ import annotations

from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Path, Response
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.core.cache import get_redis
from app.core.logging import get_logger
from app.db.models import Country, Plan
from app.domain.og_card import render_card

logger = get_logger(__name__)

router = APIRouter(prefix="/api/og", tags=["og"])

# A day. The only thing that drifts is the "from $X" price, and a share card
# that lags a repricing by hours misleads nobody — the page itself always
# shows the live number. Deliberately no invalidation hooks for that reason.
_TTL = 86400


@router.get("/{slug}.png", include_in_schema=False)
async def og_card(
    session: SessionDep,
    slug: Annotated[str, Path(max_length=120, pattern=r"^[a-z0-9-]+$")],
) -> Response:
    key = f"qs:og:{slug}"
    redis = get_redis()

    png: bytes | None = None
    try:
        png = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — a cache outage must not take the card down
        logger.warning("og.cache_read_failed", slug=slug, error=str(exc))

    if png is None:
        row = (
            await session.execute(
                select(Country.name, Country.name_uz)
                .where(Country.slug == slug, Country.is_active.is_(True))
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown destination")

        price = (
            await session.execute(
                select(func.min(Plan.price_usd))
                .join(Country, Country.id == Plan.country_id)
                .where(Country.slug == slug, Plan.is_active.is_(True), Plan.price_usd > 0)
            )
        ).scalar()

        # The card is what a *sharer's* audience sees, and this audience is
        # Uzbek-speaking — so the localised name, with the English catalogue
        # name as the fallback the storefront itself uses.
        name = row.name_uz or row.name
        # Pillow is synchronous; a render is ~20 ms but there is no reason to
        # hold the event loop for it when a thread is free.
        png = await anyio.to_thread.run_sync(render_card, name, price)

        try:
            await redis.set(key, png, ex=_TTL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("og.cache_write_failed", slug=slug, error=str(exc))

    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={_TTL}"},
    )
