from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Region(Base):
    __tablename__ = "catalog_region"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    name_ru: Mapped[str] = mapped_column(String(80), default="")
    name_uz: Mapped[str] = mapped_column(String(80), default="")
    slug: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    countries: Mapped[list[Country]] = relationship(back_populates="region")
    # Multi-country tariffs — a "Europe, 41 countries" eSIM belongs to a region
    # and to no country. Both foreign keys point at catalog_region, so the join
    # has to be spelled out or SQLAlchemy cannot tell which one this is.
    plans: Mapped[list[Plan]] = relationship(
        "Plan", primaryjoin="Region.id == foreign(Plan.region_id)", viewonly=True
    )


class Country(Base):
    __tablename__ = "catalog_country"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    name_ru: Mapped[str] = mapped_column(String(120), default="")
    name_uz: Mapped[str] = mapped_column(String(120), default="")
    slug: Mapped[str] = mapped_column(String(120))
    iso2: Mapped[str] = mapped_column(String(2))
    region_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_region.id"))
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    region: Mapped[Region | None] = relationship(back_populates="countries")
    plans: Mapped[list[Plan]] = relationship(back_populates="country")


class Plan(Base):
    __tablename__ = "catalog_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(10), default="local")
    country_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_country.id"))
    region_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_region.id"))
    title: Mapped[str] = mapped_column(String(120))
    data_amount_mb: Mapped[int] = mapped_column(Integer, default=1024)
    is_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    validity_days: Mapped[int] = mapped_column(Integer, default=7)
    # Supplier cost and markup are internal: they are mapped so workers and
    # reports can read them, but they must never reach a customer-facing
    # schema. `tests/integration/test_api_smoke.py` enforces that.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    markup_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    price_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Decimal, not float: money must never round-trip through binary floating point.
    price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Customer-facing text beside the price — "+ deposit" and the like. Never
    # parsed: every calculation reads price_usd, so this can say anything
    # without putting arithmetic at risk.
    price_note: Mapped[str] = mapped_column(String(120), default="")
    network_type: Mapped[str] = mapped_column(String(2), default="4G")
    supports_hotspot: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_package_code: Mapped[str] = mapped_column(String(120), default="")
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    country: Mapped[Country | None] = relationship(back_populates="plans")
    region: Mapped[Region | None] = relationship()
    offers: Mapped[list[SupplierOffer]] = relationship(
        back_populates="plan", order_by="SupplierOffer.cost_usd"
    )

    @property
    def fallback_offers(self) -> list[SupplierOffer]:
        """Usable offers other than the one this plan is currently routed to.

        Cheapest first. Fulfilment walks this when the primary supplier
        refuses, so a single supplier outage does not strand a paid order.
        """
        return [
            offer
            for offer in sorted(self.offers, key=lambda o: (o.cost_usd, o.provider))
            if offer.is_available and offer.provider != self.provider
        ]

    @property
    def data_label(self) -> str:
        if self.is_unlimited:
            return "Unlimited"
        if self.data_amount_mb % 1024 == 0:
            return f"{self.data_amount_mb // 1024} GB"
        return f"{self.data_amount_mb} MB"


class SupplierOffer(Base):
    """One supplier's wholesale price for a plan. Owned by the Django admin.

    Read-only here. The winning offer is already denormalised onto
    `Plan.provider` / `Plan.cost_usd`, so pricing and the storefront never need
    to consult this table; fulfilment reads it only to find a fallback route
    when the primary supplier refuses an order.
    """

    __tablename__ = "catalog_supplieroffer"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    provider: Mapped[str] = mapped_column(String(20))
    package_code: Mapped[str] = mapped_column(String(120))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    unavailable_reason: Mapped[str] = mapped_column(String(200), default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    plan: Mapped[Plan] = relationship(back_populates="offers")
