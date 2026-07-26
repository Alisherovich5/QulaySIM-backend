"""SQLAlchemy models mirroring the Django-owned PostgreSQL schema.

Django owns migrations; these classes map the exact same tables/columns so the
FastAPI service can read and write the same database. Keep column names in sync
with admin/*/models.py.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Region(Base):
    __tablename__ = "catalog_region"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    countries: Mapped[list["Country"]] = relationship(back_populates="region")


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

    region: Mapped["Region | None"] = relationship(back_populates="countries")
    plans: Mapped[list["Plan"]] = relationship(back_populates="country")


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
    price_usd: Mapped[float] = mapped_column(Numeric(8, 2))
    network_type: Mapped[str] = mapped_column(String(2), default="4G")
    supports_hotspot: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_package_code: Mapped[str] = mapped_column(String(120), default="")
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    country: Mapped["Country | None"] = relationship(back_populates="plans")
    region: Mapped["Region | None"] = relationship()

    @property
    def data_label(self) -> str:
        if self.is_unlimited:
            return "Unlimited"
        if self.data_amount_mb % 1024 == 0:
            return f"{self.data_amount_mb // 1024} GB"
        return f"{self.data_amount_mb} MB"


class Customer(Base):
    __tablename__ = "customers_customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    full_name: Mapped[str] = mapped_column(String(150), default="")
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    referral_code: Mapped[str | None] = mapped_column(String(12), unique=True, nullable=True)
    referred_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), nullable=True
    )


class PromoCode(Base):
    __tablename__ = "orders_promocode"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    discount_type: Mapped[str] = mapped_column(String(10), default="percent")
    discount_value: Mapped[float] = mapped_column(Numeric(8, 2))
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Order(Base):
    __tablename__ = "orders_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    discount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    promo_code_id: Mapped[int | None] = mapped_column(ForeignKey("orders_promocode.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_transaction_id: Mapped[str] = mapped_column(String(64), default="")
    provider_order_no: Mapped[str] = mapped_column(String(64), default="")
    provider_status: Mapped[str] = mapped_column(String(40), default="")

    customer: Mapped["Customer"] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    esims: Mapped[list["ESIM"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "orders_orderitem"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    unit_price: Mapped[float] = mapped_column(Numeric(8, 2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    order: Mapped["Order"] = relationship(back_populates="items")
    plan: Mapped["Plan"] = relationship()


class ESIM(Base):
    __tablename__ = "orders_esim"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    iccid: Mapped[str] = mapped_column(String(22), unique=True)
    qr_payload: Mapped[str] = mapped_column(String(255))
    qr_image: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_esim_tran_no: Mapped[str] = mapped_column(String(64), default="")
    provider_status: Mapped[str] = mapped_column(String(40), default="")
    provider_qr_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending")
    data_total_mb: Mapped[int] = mapped_column(Integer, default=0)
    data_used_mb: Mapped[int] = mapped_column(Integer, default=0)
    validity_days: Mapped[int] = mapped_column(Integer, default=7)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    order: Mapped["Order"] = relationship(back_populates="esims")
    plan: Mapped["Plan"] = relationship()


class Payment(Base):
    __tablename__ = "orders_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    method: Mapped[str] = mapped_column(String(30), default="mock")
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(10), default="success")
    provider_ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FAQ(Base):
    __tablename__ = "content_faq"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(255))
    answer: Mapped[str] = mapped_column(Text)
    question_ru: Mapped[str] = mapped_column(String(255), default="")
    answer_ru: Mapped[str] = mapped_column(Text, default="")
    question_uz: Mapped[str] = mapped_column(String(255), default="")
    answer_uz: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(20), default="general")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Banner(Base):
    __tablename__ = "content_banner"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    subtitle: Mapped[str] = mapped_column(String(255), default="")
    image_url: Mapped[str] = mapped_column(String(200), default="")
    cta_text: Mapped[str] = mapped_column(String(60), default="")
    cta_link: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Benefit(Base):
    __tablename__ = "content_benefit"

    id: Mapped[int] = mapped_column(primary_key=True)
    icon: Mapped[str] = mapped_column(String(40), default="Globe2")
    title: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    title_ru: Mapped[str] = mapped_column(String(120), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    title_uz: Mapped[str] = mapped_column(String(120), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Testimonial(Base):
    __tablename__ = "content_testimonial"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(80))
    location: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    location_ru: Mapped[str] = mapped_column(String(120), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    location_uz: Mapped[str] = mapped_column(String(120), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    rating: Mapped[int] = mapped_column(Integer, default=5)
    moderation_status: Mapped[str] = mapped_column(String(10), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Device(Base):
    __tablename__ = "content_device"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoBanner(Base):
    __tablename__ = "content_promobanner"

    id: Mapped[int] = mapped_column(primary_key=True)
    eyebrow: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    eyebrow_ru: Mapped[str] = mapped_column(String(80), default="")
    title_ru: Mapped[str] = mapped_column(String(160), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    eyebrow_uz: Mapped[str] = mapped_column(String(80), default="")
    title_uz: Mapped[str] = mapped_column(String(160), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    code: Mapped[str] = mapped_column(String(40), default="WELCOME10")
    cta_link: Mapped[str] = mapped_column(String(255), default="/destinations")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Referral(Base):
    __tablename__ = "customers_referral"

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    referred_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), nullable=True
    )
    referred_email: Mapped[str] = mapped_column(String(254), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending")
    reward_code: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
