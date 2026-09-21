"""Countries, tariffs, and the importer that fills them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff, OwnerOnly
from app.core.cache import get_redis
from app.core.errors import NotFoundError, ServiceUnavailableError, ValidationError
from app.core.logging import get_logger
from app.db.models import CatalogSyncRun, Country, Plan, PricingRule, SupplierOffer
from app.services.currency import charm_uzs, usd_to_uzs

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/backoffice", tags=["backoffice"])

# Older than this and the catalogue is treated as stale on the dashboard. The
# importer runs nightly, so a day and a half means it has missed one and nobody
# noticed.
STALE_AFTER = timedelta(hours=36)

# Where a "run it now" request is left for the importer container to find.
# Ten minutes: long enough for its once-a-minute check, short enough that a
# request made and forgotten does not start an import in the evening.
SYNC_REQUEST_KEY = "qs:catalog:sync_request"
SYNC_REQUEST_TTL = 600


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
    """Ask the importer to run now instead of at its nightly hour.

    A request, not a call. The importer is a loop in its own container that
    walks several thousand supplier packages over about five minutes; this
    leaves a note in Redis and that loop picks it up within the minute.

    It used to publish a Celery task by a name no worker had ever registered,
    so the button reported "import started" and nothing whatsoever happened —
    which is worse than no button, because it answers the question wrongly.
    """
    if payload.mode not in ("dry", "apply"):
        raise ValidationError("mode faqat 'dry' yoki 'apply' bo‘lishi mumkin")

    try:
        client = get_redis()
        # The value says what to run; the TTL means a request nobody collected
        # expires instead of firing an import hours later for no reason.
        await client.setex(SYNC_REQUEST_KEY, SYNC_REQUEST_TTL, payload.mode)
    except Exception as exc:
        logger.warning("backoffice.sync_request_failed", error=str(exc))
        raise ServiceUnavailableError("So‘rov yozilmadi — Redis javob bermadi") from exc

    logger.info("backoffice.sync_requested", staff=staff.username, mode=payload.mode)
    return {
        "summary": (
            "Sinov so‘raldi — bir daqiqa ichida boshlanadi, natija tarixda ko‘rinadi."
            if payload.mode == "dry"
            else "Import so‘raldi — bir daqiqa ichida boshlanadi, natija tarixda ko‘rinadi."
        )
    }


class RuleRow(BaseModel):
    id: int
    scope: str
    provider: str
    country: str | None
    country_iso2: str | None
    markup_percent: float
    tier_data_mb: int | None
    tier_days: int | None
    min_margin_usd: float
    rounding: str
    is_active: bool
    note: str
    updated_at: datetime


@router.get("/pricing-rules")
async def pricing_rules(session: SessionDep, staff: CurrentStaff) -> dict[str, object]:
    """The markup ladder, most specific first.

    Order matters and is the whole point: the narrowest rule that matches a
    tariff wins, so a list sorted by anything else reads as a set of equals and
    hides which one is actually in force.
    """
    rows = (
        await session.execute(
            select(PricingRule, Country.name_uz, Country.name, Country.iso2)
            .outerjoin(Country, Country.id == PricingRule.country_id)
            .order_by(
                PricingRule.is_active.desc(),
                PricingRule.country_id.isnot(None).desc(),
                PricingRule.tier_data_mb.isnot(None).desc(),
                PricingRule.provider != "",
                PricingRule.id,
            )
        )
    ).all()
    return {
        "items": [
            RuleRow(
                id=r.id,
                scope=r.scope,
                provider=r.provider,
                country=(uz or name) if name else None,
                country_iso2=iso2,
                markup_percent=float(r.markup_percent),
                tier_data_mb=r.tier_data_mb,
                tier_days=r.tier_days,
                min_margin_usd=float(r.min_margin_usd),
                rounding=r.rounding,
                is_active=r.is_active,
                note=r.note,
                updated_at=r.updated_at,
            )
            for r, uz, name, iso2 in rows
        ]
    }


class RulePatch(BaseModel):
    markup_percent: float | None = Field(default=None, ge=0, le=500)
    min_margin_usd: float | None = Field(default=None, ge=0, le=1000)
    rounding: str | None = Field(default=None, pattern="^(charm|whole|half|none)$")
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=200)


@router.patch("/pricing-rules/{rule_id}", status_code=204)
async def edit_rule(
    rule_id: int, payload: RulePatch, session: SessionDep, staff: OwnerOnly
) -> None:
    """Change one rule. Owner only — this is the shop's margin.

    The new markup reaches the shelf at the next catalogue import, which is
    where prices are recomputed; it is not applied here. Repricing 1,435
    tariffs inside a web request, against a copy of the pricing rules written
    in a second language, is how two markups drift apart.
    """
    rule = await session.get(PricingRule, rule_id)
    if rule is None:
        raise NotFoundError("Qoida topilmadi")

    values: dict[str, object] = {}
    if payload.markup_percent is not None:
        values["markup_percent"] = Decimal(str(payload.markup_percent))
    if payload.min_margin_usd is not None:
        values["min_margin_usd"] = Decimal(str(payload.min_margin_usd))
    if payload.rounding is not None:
        values["rounding"] = payload.rounding
    if payload.is_active is not None:
        values["is_active"] = payload.is_active
    if payload.note is not None:
        values["note"] = payload.note.strip()
    if not values:
        return

    values["updated_at"] = datetime.now(UTC)
    await session.execute(update(PricingRule).where(PricingRule.id == rule_id).values(**values))
    await session.commit()
    logger.info(
        "backoffice.pricing_rule_edited",
        staff=staff.username,
        rule_id=rule_id,
        **{k: str(v) for k, v in values.items() if k != "updated_at"},
    )


class OfferRow(BaseModel):
    id: int
    plan_id: int
    plan_title: str
    country: str | None
    provider: str
    package_code: str
    cost_usd: float
    is_available: bool
    unavailable_reason: str
    last_synced_at: datetime | None


@router.get("/offers")
async def list_offers(
    session: SessionDep,
    staff: CurrentStaff,
    provider: str = "",
    q: str = "",
    holat: str = "",
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """What each wholesaler quotes for each tariff.

    Two thousand rows, so this is filtered rather than listed: by supplier, by
    availability, or by a fragment of the tariff name or package code — which
    is how somebody arrives here, holding a code from a failed purchase.
    """
    where = []
    if provider:
        where.append(SupplierOffer.provider == provider)
    if holat == "available":
        where.append(SupplierOffer.is_available.is_(True))
    elif holat == "unavailable":
        where.append(SupplierOffer.is_available.is_(False))
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(Plan.title).like(needle),
                func.lower(SupplierOffer.package_code).like(needle),
            )
        )

    total = (
        await session.execute(
            select(func.count())
            .select_from(SupplierOffer)
            .join(Plan, Plan.id == SupplierOffer.plan_id)
            .where(*where)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(SupplierOffer, Plan.title, Country.name_uz, Country.name)
            .join(Plan, Plan.id == SupplierOffer.plan_id)
            .outerjoin(Country, Country.id == Plan.country_id)
            .where(*where)
            .order_by(SupplierOffer.is_available, SupplierOffer.updated_at.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    ).all()

    return {
        "items": [
            OfferRow(
                id=o.id,
                plan_id=o.plan_id,
                plan_title=title,
                country=(uz or name) if name else None,
                provider=o.provider,
                package_code=o.package_code,
                cost_usd=float(o.cost_usd),
                is_available=o.is_available,
                unavailable_reason=o.unavailable_reason,
                last_synced_at=o.last_synced_at,
            )
            for o, title, uz, name in rows
        ],
        "total": int(total),
        "page": page,
        "pages": max(1, -(-int(total) // size)),
    }


class OfferPatch(BaseModel):
    is_available: bool
    reason: str = Field(default="", max_length=200)


@router.patch("/offers/{offer_id}", status_code=204)
async def edit_offer(
    offer_id: int, payload: OfferPatch, session: SessionDep, staff: OwnerOnly
) -> None:
    """Take one supplier's quote out of the running, or put it back.

    Fulfilment routes an order to the cheapest available offer. Marking one
    unavailable is what you do when a wholesaler keeps refusing a package: the
    next-cheapest takes over instead of every order for that tariff failing.
    """
    offer = await session.get(SupplierOffer, offer_id)
    if offer is None:
        raise NotFoundError("Taklif topilmadi")
    await session.execute(
        update(SupplierOffer)
        .where(SupplierOffer.id == offer_id)
        .values(
            is_available=payload.is_available,
            unavailable_reason="" if payload.is_available else payload.reason.strip(),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()
    logger.info(
        "backoffice.offer_toggled",
        staff=staff.username,
        offer_id=offer_id,
        is_available=payload.is_available,
    )
