from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.db.models.catalog import Plan
from app.db.models.customers import Customer
from app.db.types import EncryptedString, EncryptedText


class PromoCode(Base):
    __tablename__ = "orders_promocode"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    discount_type: Mapped[str] = mapped_column(String(10), default="percent")
    discount_value: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    min_order_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Why the code exists. Cashback codes are minted by the worker, never typed.
    reason: Mapped[str] = mapped_column(String(10), default="manual")
    # When set, only this customer may redeem it. Cashback is earned by a person;
    # an unbound code posted in a group chat discounts everyone's order, which is
    # how a loyalty scheme turns into a site-wide sale nobody approved.
    issued_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), nullable=True
    )
    # Redeemable only by someone who has never paid. WELCOME10 is advertised as
    # a discount on your first eSIM and applied to every later one too, so the
    # same customer kept getting 10% off indefinitely.
    first_order_only: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # Given away by staff at our cost rather than sold. The reports read this:
    # counted as spend, never as revenue, because nobody paid.
    is_complimentary: Mapped[bool] = mapped_column(Boolean, default=False)
    # Frozen at checkout — see the Django model for why.
    amount_uzs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
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
    # What the supplier charged at the moment of sale. Frozen here because the
    # plan's live cost moves with every repricing sync — joining to it later
    # silently rewrites the margin history the dashboard reports. Nullable:
    # rows sold before this column existed have no snapshot to claim.
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    order: Mapped[Order] = relationship(back_populates="items")
    plan: Mapped[Plan] = relationship()


class ESIM(Base):
    __tablename__ = "orders_esim"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("catalog_plan.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers_customer.id"))
    # ICCID stays in the clear so support can look a profile up; on its own it
    # installs nothing. The activation code is the credential, so it does not.
    iccid: Mapped[str] = mapped_column(String(22), unique=True)
    qr_payload: Mapped[str] = mapped_column(EncryptedString)
    qr_image: Mapped[str] = mapped_column(EncryptedText, default="")
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

    def _sold_line(self) -> OrderItem | None:
        """The order line this eSIM was sold on, or None if it is not loaded.

        Deliberately refuses to fetch. These properties are read by Pydantic
        while serialising a response, which happens outside the greenlet the
        async engine needs — a lazy load there does not fetch, it raises
        MissingGreenlet and turns the whole endpoint into a 500. That is exactly
        what `/account/orders` did: it embeds eSIMs, and eager-loading was set
        up on the eSIM listing but not on the order listing.

        So an unloaded relationship reports "unknown price" and the response
        still renders. The repositories eager-load it where the price matters.
        """
        state = sa_inspect(self)
        if "order" in state.unloaded:
            return None
        order = self.order
        if order is None or "items" in sa_inspect(order).unloaded:
            return None
        for item in order.items:
            if item.plan_id == self.plan_id:
                return item
        return None

    @property
    def paid_usd(self) -> Decimal | None:
        """What this eSIM cost when it was bought, not what its plan costs now.

        Read from the order line, which freezes `unit_price` at the moment of
        sale. Joining to the plan instead would rewrite history every time the
        catalogue reprices — a customer opening their account after a price
        change would be told they paid today's number.

        None when the line cannot be found: an eSIM issued by hand, or a plan
        removed from the order. A missing price is shown as missing rather than
        guessed at.

        Requires `order.items` to be eager-loaded; the repositories do that.
        """
        line = self._sold_line()
        return line.unit_price if line is not None else None

    @property
    def paid_uzs(self) -> Decimal | None:
        """The same amount in som, at the rate frozen on the order.

        Converted with the order's own `exchange_rate` rather than today's, so
        the figure still matches the receipt months later. Rounded with the same
        charm rule the storefront and checkout use, which makes it identical to
        what was actually charged on a single-line order and the line's fair
        share on a larger one.
        """
        from app.services.currency import charm_uzs

        line = self._sold_line()
        if line is None:
            return None
        usd = line.unit_price
        rate = line.order.exchange_rate
        if usd is None or rate is None:
            return None
        return charm_uzs((usd * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Payment(Base):
    __tablename__ = "orders_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    method: Mapped[str] = mapped_column(String(30), default="mock")
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(10), default="success")
    provider_ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymeTransaction(Base):
    """Mirrors orders_paymetransaction — see the Django model for the state
    machine. States are Payme's: 1 created, 2 performed, -1 cancelled,
    -2 cancelled after perform."""

    __tablename__ = "orders_paymetransaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    account: Mapped[str] = mapped_column(String(64))
    state: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[int | None] = mapped_column(Integer, nullable=True)
    create_time: Mapped[int] = mapped_column(BigInteger, default=0)
    perform_time: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_time: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship()


class SupplierPurchase(Base):
    """Mirrors orders_supplierpurchase — the Django model owns the schema.

    The one thing standing between a Celery retry and a second eSIM bought at
    our own expense. eSIM Access deduplicates by the transaction id we hand it;
    eSIMCard's purchase endpoint takes a package id and nothing else, so a
    repeated call is a repeated purchase. The unique key over
    (order, provider, line_key) is the lock: fulfilment inserts a claim before
    it spends money, and a retry hits the constraint instead of the supplier.

    `state` records what became of the claim — see the Django model for why a
    row stuck in "claimed" is never retried automatically.
    """

    __tablename__ = "orders_supplierpurchase"
    __table_args__ = (
        UniqueConstraint(
            "order_id", "provider", "line_key", name="uniq_supplier_purchase_per_unit"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    provider: Mapped[str] = mapped_column(String(20))
    line_key: Mapped[str] = mapped_column(String(40))
    package_code: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(10), default="claimed")
    supplier_ref: Mapped[str] = mapped_column(String(120), default="")
    iccid: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AtmosTransaction(Base):
    """Mirrors orders_atmostransaction — the Django model owns the schema.

    Unlike Payme there is no provider-driven state machine: a callback either
    confirmed the order or was rejected, and that verdict never changes from
    our side. transaction_id is unique so a retried callback collides here
    instead of double-recording a payment.
    """

    __tablename__ = "orders_atmostransaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders_order.id"))
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    account: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship()
