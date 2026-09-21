"""Tables the backoffice reads that no customer-facing endpoint touches.

Django created every one of them and — for now — still owns their migrations.
Mapping them here is what lets the backoffice run on FastAPI while the schema
stays exactly where it is: no copy of the data, no second source of truth, and
the same rows the Django admin was reading an hour ago.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

# Postgres owns these columns; the test suite builds the same tables on SQLite,
# which has neither type. The variant keeps one model definition working on
# both rather than a second set of models for tests to lie with.
_INET = INET().with_variant(String(45), "sqlite")
_JSONB = JSONB().with_variant(JSON, "sqlite")


class Staff(Base):
    """auth_user. The people who sign in to the backoffice.

    `password` holds a Django hash (pbkdf2_sha256$…), which is why the
    backoffice verifies passwords through its own verifier rather than the
    bcrypt one the storefront uses: the accounts predate this service and the
    people using them must keep the password they already have.
    """

    __tablename__ = "auth_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    password: Mapped[str] = mapped_column(String(128))
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    first_name: Mapped[str] = mapped_column(String(150), default="")
    last_name: Mapped[str] = mapped_column(String(150), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    date_joined: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username

    @property
    def role(self) -> str:
        return "owner" if self.is_superuser else "operator"


class TOTPDevice(Base):
    """otp_totp_totpdevice. One confirmed device per person is the rule the
    backoffice enforces; django-otp allows several and we simply accept any of
    them, so an operator who enrolled twice is not locked out."""

    __tablename__ = "otp_totp_totpdevice"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_user.id"))
    name: Mapped[str] = mapped_column(String(64), default="")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    key: Mapped[str] = mapped_column(String(80))
    step: Mapped[int] = mapped_column(SmallInteger, default=30)
    digits: Mapped[int] = mapped_column(SmallInteger, default=6)
    tolerance: Mapped[int] = mapped_column(SmallInteger, default=1)
    drift: Mapped[int] = mapped_column(SmallInteger, default=0)
    # The last counter accepted. Writing it back is what stops a code being
    # replayed inside its own 30-second window.
    last_t: Mapped[int] = mapped_column(BigInteger, default=-1)
    throttling_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    throttling_failure_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessLog(Base):
    """axes_accesslog — a successful sign-in. django-axes wrote these; the
    backoffice writes its own now, to the same table, so the journal does not
    start over on the day Django's login page stopped being used."""

    __tablename__ = "axes_accesslog"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str | None] = mapped_column(_INET, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255))
    http_accept: Mapped[str] = mapped_column(String(1025), default="")
    path_info: Mapped[str] = mapped_column(String(255), default="")
    attempt_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    logout_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_hash: Mapped[str] = mapped_column(String(64), default="")


class AccessFailureLog(Base):
    """axes_accessfailurelog — a refused sign-in."""

    __tablename__ = "axes_accessfailurelog"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str | None] = mapped_column(_INET, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255))
    http_accept: Mapped[str] = mapped_column(String(1025), default="")
    path_info: Mapped[str] = mapped_column(String(255), default="")
    attempt_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_out: Mapped[bool] = mapped_column(Boolean, default=False)


class RowChange(Base):
    """audit_row_change — the database's own trigger-written history. Nothing
    in the application writes it, which is the point: it records what happened
    even when it happened by hand in psql."""

    __tablename__ = "audit_row_change"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    db_user: Mapped[str] = mapped_column(Text)
    app_name: Mapped[str | None] = mapped_column(Text)
    client_addr: Mapped[str | None] = mapped_column(_INET, nullable=True)
    tbl: Mapped[str] = mapped_column(Text)
    op: Mapped[str] = mapped_column(Text)
    row_pk: Mapped[str | None] = mapped_column(Text)
    old_row: Mapped[dict[str, Any] | None] = mapped_column(_JSONB, nullable=True)
    new_row: Mapped[dict[str, Any] | None] = mapped_column(_JSONB, nullable=True)


class CatalogSyncRun(Base):
    """catalog_catalogsyncrun — one importer pass."""

    __tablename__ = "catalog_catalogsyncrun"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    packages_read: Mapped[int] = mapped_column(Integer, default=0)
    countries_created: Mapped[int] = mapped_column(Integer, default=0)
    plans_created: Mapped[int] = mapped_column(Integer, default=0)
    offers_written: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[str] = mapped_column(Text, default="")


class ComplimentaryGrant(Base):
    """orders_complimentarygrant — an eSIM handed over at our cost."""

    __tablename__ = "orders_complimentarygrant"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders_order.id"))
    granted_by_id: Mapped[int | None] = mapped_column(ForeignKey("auth_user.id"))
    reason: Mapped[str] = mapped_column(String(200), default="")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
