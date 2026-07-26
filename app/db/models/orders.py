from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.db.models.catalog import Plan
from app.db.models.customers import Customer


class PromoCode(Base):
    __tablename__ = "orders_promocode"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    discount_type: Mapped[str] = mapped_column(String(10), default="percent")
    discount_value: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Order(Base):
    __tablename__ = "orders_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    promo_code_id: Mapped[int | None] = mapped_column(ForeignKey("orders_promocode.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_transaction_id: Mapped[str] = mapped_column(String(64), default="")
    provider_order_no: Mapped[str] = mapped_column(String(64), default="")
    provider_status: Mapped[str] = mapped_column(String(40), default="")

    customer: Mapped[Customer] = relationship()
    items: Mapped[list[OrderItem]] = relationship(back_populates="order")
    esims: Mapped[list[ESIM]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "orders_orderitem"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    order: Mapped[Order] = relationship(back_populates="items")
    plan: Mapped[Plan] = relationship()


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="esims")
    plan: Mapped[Plan] = relationship()


class Payment(Base):
    __tablename__ = "orders_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    method: Mapped[str] = mapped_column(String(30), default="mock")
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(10), default="success")
    provider_ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
