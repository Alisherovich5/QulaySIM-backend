"""Countries, tariffs, and the importer that fills them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff, OwnerOnly
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.db.models import CatalogSyncRun, Country, Plan
from app.services.currency import charm_uzs, usd_to_uzs

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])

# Older than this and the catalogue is treated as stale on the dashboard. The
# importer runs nightly, so a day and a half means it has missed one and nobody
# noticed.
STALE_AFTER = timedelta(hours=36)


class CountryOut(BaseModel):
    id: int
    name: str
    iso2: str
    active_plans: int


class CountryFull(CountryOut):
    name_ru: str
    name_en: str
    is_popular: bool


class PlanOut(BaseModel):
    id: int
    data_label: str
    validity_days: int
    cost_usd: float
    price_usd: float
    price_uzs: int
    provider: str
    is_active: bool
    is_unlimited: bool


class GrantablePlan(BaseModel):
    id: int
    title: str
    cost_usd: float
    provider: str
    country_iso2: str
    data_label: str
    days: int


async def _counts(session: SessionDep) -> dict[int, int]:
    rows = (
        await session.execute(
            select(Plan.country_id, func.count())
            .where(Plan.is_active.is_(True), Plan.country_id.isnot(None))
            .group_by(Plan.country_id)
        )
    ).all()
    return {int(row[0]): int(row[1]) for row in rows}


@router.get("/countries")
async def list_countries(
    session: SessionDep, staff: CurrentStaff, full: int = 0
) -> list[CountryOut] | dict[str, list[CountryFull]]:
    countries = list(
        (await session.execute(select(Country).order_by(Country.name))).scalars().all()
    )
    counts = await _counts(session)
    if not full:
        return [
            CountryOut(
                id=c.id, name=c.name_uz or c.name, iso2=c.iso2, active_plans=counts.get(c.id, 0)
            )
            for c in countries
        ]
    return {
        "items": [
            CountryFull(
                id=c.id,
                name=c.name_uz or c.name,
                name_ru=c.name_ru,
                name_en=c.name,
                iso2=c.iso2,
                active_plans=counts.get(c.id, 0),
                is_popular=c.is_popular,
            )
            for c in countries
        ]
    }


@router.get("/plans")
async def list_plans(
    country: str, session: SessionDep, staff: CurrentStaff
) -> dict[str, list[PlanOut]]:
    # One rate for the whole page. Pricing each row separately would show two
    # tariffs converted at different rates if the cache turned over mid-render.
    rate = Decimal(str((await usd_to_uzs())["usd_to_uzs"]))
    rows = list(
        (
            await session.execute(
                select(Plan)
                .join(Country, Country.id == Plan.country_id)
                .where(func.upper(Country.iso2) == country.upper())
                .order_by(Plan.is_unlimited, Plan.data_amount_mb, Plan.validity_days)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            PlanOut(
                id=p.id,
                data_label=p.data_label,
                validity_days=p.validity_days,
                cost_usd=float(p.cost_usd or 0),
                price_usd=float(p.price_usd),
                price_uzs=int(charm_uzs(p.price_usd * rate)),
                provider=p.provider,
                is_active=p.is_active,
                is_unlimited=p.is_unlimited,
            )
            for p in rows
        ]
    }


@router.get("/plans/grantable", response_model=list[GrantablePlan])
async def grantable(davlat: str, session: SessionDep, staff: CurrentStaff) -> list[GrantablePlan]:
    """What can be handed over for free, in one country.

    Only active plans with a real supplier behind them: a "mock" plan would
    produce an eSIM that installs nothing, which is a worse outcome than
    telling the operator there is nothing to give.
    """
    rows = list(
        (
            await session.execute(
                select(Plan, Country.iso2)
                .join(Country, Country.id == Plan.country_id)
                .where(
                    func.upper(Country.iso2) == davlat.upper(),
                    Plan.is_active.is_(True),
                    Plan.provider != "mock",
                )
                .order_by(Plan.is_unlimited, Plan.data_amount_mb, Plan.validity_days)
            )
        ).all()
    )
    return [
        GrantablePlan(
            id=plan.id,
            title=plan.title,
            cost_usd=float(plan.cost_usd or 0),
            provider=plan.provider,
            country_iso2=iso2,
            data_label=plan.data_label,
            days=plan.validity_days,
        )
        for plan, iso2 in rows
    ]


class ActiveIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    is_active: bool


@router.post("/plans/active", status_code=204)
async def set_active(payload: ActiveIn, session: SessionDep, staff: OwnerOnly) -> None:
    """Turn tariffs on or off in bulk.

    Owner only. Switching a tariff off takes it out of the shop immediately,
    and switching the wrong hundred off is a silent outage on the storefront.
    """
    await session.execute(
        update(Plan).where(Plan.id.in_(payload.ids)).values(is_active=payload.is_active)
    )
    await session.commit()
    logger.info(
        "backoffice.plans_toggled",
        staff=staff.username,
        count=len(payload.ids),
        is_active=payload.is_active,
    )


class SyncRun(BaseModel):
    id: int
    started_at: datetime
    provider: str
    packages_read: int
    new_plans: int
    new_offers: int
    ok: bool
    note: str


class SyncOut(BaseModel):
    items: list[SyncRun]
    last_ok_at: datetime | None
    stale: bool


@router.get("/sync", response_model=SyncOut)
async def sync_history(session: SessionDep, staff: CurrentStaff) -> SyncOut:
    rows = list(
        (
            await session.execute(
                select(CatalogSyncRun).order_by(CatalogSyncRun.started_at.desc()).limit(30)
            )
        )
        .scalars()
        .all()
    )
    last_ok = (
        await session.execute(
            select(func.max(CatalogSyncRun.finished_at)).where(CatalogSyncRun.status == "ok")
        )
    ).scalar_one_or_none()
    return SyncOut(
        items=[
            SyncRun(
                id=r.id,
                started_at=r.started_at,
                provider=r.provider,
                packages_read=r.packages_read,
                new_plans=r.plans_created,
                new_offers=r.offers_written,
                ok=r.status == "ok",
                note=(r.log or "").strip().splitlines()[-1] if (r.log or "").strip() else "",
            )
            for r in rows
        ],
        last_ok_at=last_ok,
        stale=last_ok is None or (datetime.now(UTC) - last_ok) > STALE_AFTER,
    )


class RunIn(BaseModel):
    mode: str = "dry"


@router.post("/sync/run")
async def run_sync(payload: RunIn, session: SessionDep, staff: OwnerOnly) -> dict[str, str]:
    """Kick the importer off by hand.

    Owner only, and never in-process: the import walks thousands of supplier
    packages and rewrites prices, which is not something to run inside a web
    request that a browser can abandon halfway.
    """
    if payload.mode not in ("dry", "apply"):
        raise ValidationError("mode faqat 'dry' yoki 'apply' bo‘lishi mumkin")

    from app.workers.celery_app import celery_app

    celery_app.send_task("catalog.import_packages", kwargs={"dry_run": payload.mode == "dry"})
    logger.info("backoffice.sync_started", staff=staff.username, mode=payload.mode)
    return {
        "summary": (
            "Sinov o‘tkazilmoqda — natija tarixda ko‘rinadi."
            if payload.mode == "dry"
            else "Import boshlandi — natija tarixda ko‘rinadi."
        )
    }
