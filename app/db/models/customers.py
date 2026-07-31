from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class Customer(Base):
    __tablename__ = "customers_customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    full_name: Mapped[str] = mapped_column(String(150), default="")
    # Empty for provider-only accounts; verify_password refuses an empty hash.
    hashed_password: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Re-encoded WebP written by this service, never the uploaded bytes.
    avatar_webp: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    referral_code: Mapped[str | None] = mapped_column(String(12), unique=True, nullable=True)
    referred_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), nullable=True
    )


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialAccount(Base):
    """A provider identity linked to a customer. Schema owned by Django.

    Keyed on the provider's own user id rather than the e-mail: an address can
    change hands at the provider, and matching on it would hand the previous
    owner's orders to whoever holds the address next.
    """

    __tablename__ = "customers_socialaccount"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    provider: Mapped[str] = mapped_column(String(20))
    provider_uid: Mapped[str] = mapped_column(String(191))
    email: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
