from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import SessionDep, language_from
from app.core.config import settings
from app.schemas.base import JSONDict
from app.schemas.content import CurrencyRateOut
from app.services import content as content_service
from app.services import currency as currency_service

router = APIRouter(prefix="/api", tags=["content"])


@router.get("/content/landing")
async def landing(
    session: SessionDep,
    language: Annotated[str, Depends(language_from)],
) -> JSONDict:
    return await content_service.landing_content(session, language)


@router.get("/currency", response_model=CurrencyRateOut)
async def currency_rate(response: Response) -> JSONDict:
    payload = await currency_service.usd_to_uzs()
    response.headers["Cache-Control"] = f"public, max-age={settings.cache_ttl_currency}"
    return payload
