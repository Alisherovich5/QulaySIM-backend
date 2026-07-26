from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Region(Base):
    __tablename__ = "catalog_region"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    countries: Mapped[list[Country]] = relationship(back_populates="region")


class Country(Base):
    __tablename__ = "catalog_country"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
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
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    markup_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    price_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Decimal, not float: money must never round-trip through binary floating point.
    price_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    network_type: Mapped[str] = mapped_column(String(2), default="4G")
    supports_hotspot: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_package_code: Mapped[str] = mapped_column(String(120), default="")
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    country: Mapped[Country | None] = relationship(back_populates="plans")
    region: Mapped[Region | None] = relationship()

    @property
    def data_label(self) -> str:
        if self.is_unlimited:
            return "Unlimited"
        if self.data_amount_mb % 1024 == 0:
            return f"{self.data_amount_mb // 1024} GB"
        return f"{self.data_amount_mb} MB"
