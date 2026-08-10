"""The numbers the shop owner needs, gathered for a window of time.

Written against the synchronous session because the only caller is a Celery
task, and Celery's prefork pool is not an asyncio context.

Money is Decimal end to end. Revenue is read from the frozen order lines rather
than from today's catalogue: `unit_price` and `unit_cost` are snapshots taken at
the sale, so a repricing between the sale and the report cannot rewrite what a
past day earned. That is the whole reason those columns exist.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ESIM, Country, Customer, Order, OrderItem, Plan
from app.db.models.enums import ESIMStatus, OrderStatus

ZERO = Decimal("0.00")

#: Days → the label the report carries. The owner asked for exactly these.
PERIOD_LABELS = {
    1: "Kunlik hisobot",
    3: "3 kunlik hisobot",
    7: "Haftalik hisobot",
    30: "Oylik hisobot",
    90: "3 oylik hisobot",
    365: "Yillik hisobot",
}


@dataclass
class SupplierLine:
    provider: str
    esims: int
    cost_usd: Decimal


@dataclass
class PurchaseLine:
    """One sale, with everything needed to recognise it without a lookup."""

    country: str
    plan: str
    provider: str
    bought_at: datetime
    expires_at: datetime | None
    paid_uzs: Decimal | None
    # Read from the plan's own columns rather than parsed back out of its title.
    # A title is a label a supplier chose; these are the numbers the customer
    # bought, and the report is asked how many GB were sold.
    data_mb: int = 0
    days: int = 0


#: How many individual sales a report lists before it summarises the rest.
#: Telegram caps a message at 4096 characters and a report that gets truncated
#: loses its problems section, which is the part worth reading.
MAX_LISTED = 12


@dataclass
class Report:
    label: str
    since: datetime
    until: datetime

    orders: int = 0
    total_data_mb: int = 0
    revenue_usd: Decimal = ZERO
    revenue_uzs: Decimal = ZERO
    cost_usd: Decimal = ZERO

    suppliers: list[SupplierLine] = field(default_factory=list)
    top_countries: list[tuple[str, int]] = field(default_factory=list)
    purchases: list[PurchaseLine] = field(default_factory=list)
    expired: list[PurchaseLine] = field(default_factory=list)

    new_customers: int = 0
    buying_customers: int = 0

    unfulfilled_orders: list[int] = field(default_factory=list)
    stuck_esims: int = 0
    abandoned_checkouts: int = 0

    @property
    def margin_usd(self) -> Decimal:
        return self.revenue_usd - self.cost_usd

    @property
    def margin_percent(self) -> Decimal:
        """Margin over cost. Zero cost means nothing to divide by, not infinity."""
        if self.cost_usd <= ZERO:
            return ZERO
        return (self.margin_usd / self.cost_usd * 100).quantize(Decimal("1"))

    @property
    def has_activity(self) -> bool:
        """Whether anything happened worth a message.

        A quiet night still sends — silence from a reporting bot is
        indistinguishable from a broken reporting bot, and the owner would only
        learn which one it was when they went looking for a number.
        """
        return bool(
            self.orders
            or self.new_customers
            or self.unfulfilled_orders
            or self.stuck_esims
            or self.expired
        )


def build_report(session: Session, *, days: int, now: datetime | None = None) -> Report:
    """Everything that happened in the last `days`, as one object."""
    until = now or datetime.now(UTC)
    since = until - timedelta(days=days)

    report = Report(
        label=PERIOD_LABELS.get(days, f"{days} kunlik hisobot"),
        since=since,
        until=until,
    )

    paid = (
        select(Order.id)
        .where(
            Order.status == OrderStatus.PAID,
            Order.paid_at.is_not(None),
            Order.paid_at >= since,
            Order.paid_at < until,
        )
        .subquery()
    )

    totals = session.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total), ZERO),
            func.coalesce(func.sum(Order.amount_uzs), ZERO),
            func.count(func.distinct(Order.customer_id)),
        ).where(Order.id.in_(select(paid.c.id)))
    ).one()
    report.orders, report.revenue_usd, report.revenue_uzs, report.buying_customers = totals

    # Cost comes from the line snapshot. Rows sold before `unit_cost` existed
    # carry NULL; they are counted as zero cost rather than dropped, which
    # overstates margin on old rows instead of hiding the sale entirely.
    report.cost_usd = session.execute(
        select(func.coalesce(func.sum(OrderItem.unit_cost * OrderItem.quantity), ZERO)).where(
            OrderItem.order_id.in_(select(paid.c.id))
        )
    ).scalar_one()

    report.total_data_mb = session.execute(
        select(func.coalesce(func.sum(ESIM.data_total_mb), 0)).where(
            ESIM.order_id.in_(select(paid.c.id))
        )
    ).scalar_one()

    supplier_rows = session.execute(
        select(
            ESIM.provider,
            func.count(ESIM.id),
            func.coalesce(func.sum(OrderItem.unit_cost * OrderItem.quantity), ZERO),
        )
        .join(
            OrderItem,
            (OrderItem.order_id == ESIM.order_id) & (OrderItem.plan_id == ESIM.plan_id),
            isouter=True,
        )
        .where(ESIM.order_id.in_(select(paid.c.id)))
        .group_by(ESIM.provider)
        .order_by(func.count(ESIM.id).desc())
    ).all()
    report.suppliers = [
        SupplierLine(provider=row[0], esims=row[1], cost_usd=row[2]) for row in supplier_rows
    ]

    report.top_countries = [
        (row[0], row[1])
        for row in session.execute(
            select(Country.name, func.count(ESIM.id))
            .join(Plan, Plan.id == ESIM.plan_id)
            .join(Country, Country.id == Plan.country_id)
            .where(ESIM.order_id.in_(select(paid.c.id)))
            .group_by(Country.name)
            .order_by(func.count(ESIM.id).desc())
            .limit(5)
        ).all()
    ]

    # Sale by sale: which tariff, from which supplier, for which country, when
    # it was bought and when it runs out. The aggregates above answer "how much";
    # this answers "what", which is the question asked when something looks off.
    report.purchases = [
        PurchaseLine(
            country=row[0] or "—",
            plan=row[1],
            provider=row[2],
            bought_at=row[3],
            expires_at=row[4],
            paid_uzs=row[5],
            data_mb=row[6] or 0,
            days=row[7] or 0,
        )
        for row in session.execute(
            select(
                Country.name,
                Plan.title,
                ESIM.provider,
                ESIM.created_at,
                ESIM.expires_at,
                Order.amount_uzs,
                ESIM.data_total_mb,
                ESIM.validity_days,
            )
            .join(Plan, Plan.id == ESIM.plan_id)
            .join(Country, Country.id == Plan.country_id, isouter=True)
            .join(Order, Order.id == ESIM.order_id)
            .where(ESIM.order_id.in_(select(paid.c.id)))
            .order_by(ESIM.created_at.desc())
        ).all()
    ]

    # Windows that closed during this period. The owner asked to be told when an
    # eSIM finishes, and a customer whose plan just ran out is the one most
    # likely to buy the next one.
    report.expired = [
        PurchaseLine(
            country=row[0] or "—",
            plan=row[1],
            provider=row[2],
            bought_at=row[3],
            expires_at=row[4],
            paid_uzs=None,
            data_mb=row[5] or 0,
            days=row[6] or 0,
        )
        for row in session.execute(
            select(
                Country.name,
                Plan.title,
                ESIM.provider,
                ESIM.created_at,
                ESIM.expires_at,
                ESIM.data_total_mb,
                ESIM.validity_days,
            )
            .join(Plan, Plan.id == ESIM.plan_id)
            .join(Country, Country.id == Plan.country_id, isouter=True)
            .where(
                ESIM.status == ESIMStatus.EXPIRED,
                ESIM.expires_at.is_not(None),
                ESIM.expires_at >= since,
                ESIM.expires_at < until,
            )
            .order_by(ESIM.expires_at.desc())
        ).all()
    ]

    report.new_customers = session.execute(
        select(func.count(Customer.id)).where(
            Customer.created_at >= since, Customer.created_at < until
        )
    ).scalar_one()

    # A paid order with no eSIM is the failure that costs money and trust at the
    # same time: the customer was charged and got nothing. Listed by id, because
    # the owner's next step is to look one up.
    report.unfulfilled_orders = [
        row[0]
        for row in session.execute(
            select(Order.id)
            .where(
                Order.id.in_(select(paid.c.id)),
                ~select(ESIM.id).where(ESIM.order_id == Order.id).exists(),
            )
            .order_by(Order.id)
        ).all()
    ]

    # Issued, but the supplier never returned a profile — the eSIM exists in our
    # database and cannot be installed.
    report.stuck_esims = session.execute(
        select(func.count(ESIM.id)).where(
            ESIM.order_id.in_(select(paid.c.id)),
            ESIM.status == ESIMStatus.PENDING,
            ESIM.provider_esim_tran_no == "",
        )
    ).scalar_one()

    # Checkout opened and never paid. Not a failure on its own, but a number
    # that climbing means the payment step is losing people.
    report.abandoned_checkouts = session.execute(
        select(func.count(Order.id)).where(
            Order.status.in_((OrderStatus.PENDING, OrderStatus.CANCELLED)),
            Order.created_at >= since,
            Order.created_at < until,
        )
    ).scalar_one()

    return report


def _escape(value: str) -> str:
    """Country and plan names reach Telegram as HTML, so they are escaped here.

    They come from supplier catalogues rather than from customers, but a
    wholesaler shipping an ampersand in a country name would break the message
    formatting rather than merely look odd.
    """
    return html.escape(value)


def _data(megabytes: int) -> str:
    """Megabytes as the figure a customer recognises.

    Whole gigabytes lose the decimal — "1 GB", not "1.0 GB" — and anything under
    a gigabyte stays in megabytes rather than becoming "0.5 GB". Zero means an
    unlimited plan, which has no number to show.
    """
    if megabytes <= 0:
        return "cheksiz"
    if megabytes % 1024 == 0:
        return f"{megabytes // 1024} GB"
    if megabytes < 1024:
        return f"{megabytes} MB"
    return f"{megabytes / 1024:.1f} GB"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def _som(value: Decimal) -> str:
    return f"{value:,.0f}".replace(",", " ")


def format_report(report: Report) -> str:
    """The report as Telegram HTML.

    Uzbek, because the people reading it run the shop in Uzbek. Kept to one
    message: a report split across several is one somebody scrolls past.
    """
    day = report.until.strftime("%d.%m.%Y %H:%M")
    lines = [
        f"<b>📊 QulaySIM — {report.label}</b>",
        f"<i>{report.since.strftime('%d.%m.%Y')} — {day}</i>",
        "",
        "<b>💰 SOTUV</b>",
    ]

    if report.orders:
        # The two numbers the owner asked for first, on one line: how many people
        # bought, and how much came in. Everything under it explains that line.
        lines += [
            f"<b>{report.buying_customers} odamga sotildi</b> · "
            f"{report.orders} ta xarid · <b>{_som(report.revenue_uzs)} so'm</b>",
            f"Jami hajm: <b>{_data(report.total_data_mb)}</b>",
            f"Dollarda: ${_money(report.revenue_usd)} · tannarx ${_money(report.cost_usd)}",
            f"Foyda: <b>${_money(report.margin_usd)}</b> ({report.margin_percent}%)",
        ]
    else:
        lines.append("Bu davrda xarid bo'lmadi.")

    if report.suppliers:
        lines += ["", "<b>🔌 TA'MINOTCHILAR</b>"]
        for supplier in report.suppliers:
            lines.append(
                f"{supplier.provider}: <b>{supplier.esims} ta</b> · ${_money(supplier.cost_usd)}"
            )

    if report.top_countries:
        lines += ["", "<b>🌍 ENG KO'P SOTILGAN</b>"]
        lines.append(" · ".join(f"{name} {count}" for name, count in report.top_countries))

    if report.purchases:
        lines += ["", "<b>🧾 XARIDLAR</b>"]
        for purchase in report.purchases[:MAX_LISTED]:
            ends = (
                purchase.expires_at.strftime("%d.%m")
                if purchase.expires_at
                else "faollashtirilmagan"
            )
            price = f" · <b>{_som(purchase.paid_uzs)} so'm</b>" if purchase.paid_uzs is not None else ""
            size = _data(purchase.data_mb)
            days = f" / {purchase.days} kun" if purchase.days else ""
            lines.append(
                f"{purchase.bought_at.strftime('%d.%m %H:%M')} · "
                f"<b>{_escape(purchase.country)}</b> · {size}{days}{price} · "
                f"{purchase.provider} · tugaydi {ends}"
            )
        if len(report.purchases) > MAX_LISTED:
            lines.append(f"<i>…va yana {len(report.purchases) - MAX_LISTED} ta</i>")

    if report.expired:
        lines += ["", "<b>⏳ MUDDATI TUGAGANLAR</b>"]
        for item in report.expired[:MAX_LISTED]:
            ended = item.expires_at.strftime("%d.%m %H:%M") if item.expires_at else "—"
            lines.append(
                f"{ended} · <b>{_escape(item.country)}</b> · {_data(item.data_mb)} · {item.provider}"
            )
        if len(report.expired) > MAX_LISTED:
            lines.append(f"<i>…va yana {len(report.expired) - MAX_LISTED} ta</i>")

    lines += [
        "",
        "<b>👤 FOYDALANUVCHILAR</b>",
        f"Yangi ro'yxatdan o'tgan: <b>{report.new_customers}</b>",
        f"Xarid qilgan: <b>{report.buying_customers}</b>",
    ]

    problems = []
    if report.unfulfilled_orders:
        ids = ", ".join(f"#{order_id}" for order_id in report.unfulfilled_orders[:10])
        problems.append(f"❗ To'landi, eSIM berilmadi: <b>{len(report.unfulfilled_orders)}</b> ({ids})")
    if report.stuck_esims:
        problems.append(f"❗ eSIM berildi, lekin o'rnatib bo'lmaydi: <b>{report.stuck_esims}</b>")
    if report.abandoned_checkouts:
        problems.append(f"To'lovga o'tib, to'lamaganlar: {report.abandoned_checkouts}")

    lines += ["", "<b>⚠️ MUAMMOLAR</b>"]
    lines += problems or ["Muammo yo'q."]

    return "\n".join(lines)


def build_sale_note(session: Session, order_id: int) -> str | None:
    """One sale, as a message to send the moment it completes.

    Returns None when the order is not a paid one, so a stray call cannot
    announce a checkout that nobody paid for.

    Reports what it finds rather than what it hopes: if the supplier has not
    returned a profile yet, the message says the eSIM is still coming instead of
    implying a delivery that has not happened.
    """
    order = session.get(Order, order_id)
    if order is None or order.status != OrderStatus.PAID:
        return None

    rows = session.execute(
        select(
            Country.name,
            Plan.title,
            OrderItem.quantity,
            OrderItem.unit_price,
            OrderItem.unit_cost,
            Plan.data_amount_mb,
            Plan.validity_days,
        )
        .join(Plan, Plan.id == OrderItem.plan_id)
        .join(Country, Country.id == Plan.country_id, isouter=True)
        .where(OrderItem.order_id == order_id)
    ).all()

    esims = session.execute(
        select(ESIM.provider, ESIM.iccid, ESIM.provider_esim_tran_no).where(
            ESIM.order_id == order_id
        )
    ).all()

    customer_email = session.execute(
        select(Customer.email).where(Customer.id == order.customer_id)
    ).scalar_one_or_none()

    revenue = sum((row[3] or ZERO) * row[2] for row in rows) or ZERO
    cost = sum((row[4] or ZERO) * row[2] for row in rows) or ZERO

    lines = [
        "<b>🎉 Yangi sotuv</b>",
        f"Buyurtma <b>#{order_id}</b> · {order.paid_at.strftime('%d.%m.%Y %H:%M') if order.paid_at else ''}",
        "",
    ]
    for row in rows:
        country, _title, quantity, unit_price, _unit_cost, data_mb, days = row
        count = f"{quantity} × " if quantity > 1 else ""
        lines.append(
            f"{count}<b>{_escape(country or '—')}</b> · {_data(data_mb)}"
            f"{f' / {days} kun' if days else ''} · ${_money(unit_price or ZERO)}"
        )

    lines += [
        "",
        f"To'landi: <b>{_som(order.amount_uzs or ZERO)} so'm</b> (${_money(revenue)})",
        f"Tannarx: ${_money(cost)} · Foyda: <b>${_money(revenue - cost)}</b>",
    ]

    if esims:
        for provider, iccid, tran_no in esims:
            state = "tayyor" if tran_no else "⏳ ta'minotchidan kutilmoqda"
            lines.append(f"{provider} · ICCID {iccid} · {state}")
    else:
        lines.append("❗ eSIM hali berilmadi")

    if customer_email:
        lines.append(f"Mijoz: {_escape(customer_email)}")

    return "\n".join(lines)
