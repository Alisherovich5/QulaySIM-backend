from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.base import JSONDict, JSONList
from app.services import catalog as service

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/regions")
async def list_regions(session: SessionDep) -> JSONList:
    return await service.list_regions(session)


@router.get("/countries")
async def list_countries(
    session: SessionDep,
    search: Annotated[str | None, Query(max_length=80)] = None,
    region: Annotated[str | None, Query(max_length=80, description="region slug")] = None,
    popular: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONList:
    return await service.list_countries(
        session,
        search=search,
        region_slug=region,
        popular=popular,
        limit=limit,
        offset=offset,
    )


@router.get("/countries/{slug}")
async def country_detail(slug: str, session: SessionDep) -> JSONDict:
    return await service.get_country(session, slug)


@router.get("/plans/popular")
async def popular_plans(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=24)] = 6,
) -> JSONList:
    """Plans marked popular in the admin, for the landing page."""
    return await service.list_popular_plans(session, limit=limit)
