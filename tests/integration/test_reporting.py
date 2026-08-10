"""The scheduled sales report.

A report is read as fact and acted on, so the failure that matters is not a
crash — it is a plausible wrong number. These tests pin the arithmetic against
rows whose answers are known by construction, and pin the two exclusions that
would otherwise inflate every figure: unpaid orders, and sales outside the
window.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.models import ESIM, Country, Customer, Order, OrderItem, Plan
from app.db.models.enums import ESIMStatus, OrderStatus
from app.core.config import settings
from app.services.reporting import build_report, format_report

pytestmark = pytest.mark.anyio


@contextmanager
def worker_session():
    """A synchronous session, the way the Celery task gets one.

    Not `app.workers.session.worker_session` only because of a local-development
    quirk: psycopg resolves "localhost" to ::1 first, and a developer's Postgres
    usually listens on IPv4 alone, so the shared engine cannot connect from a
    test run while asyncpg can. In Docker the host is a service name and the
    question never arises. Same driver, same URL, host pinned.
    """
    url = settings.sync_database_url.replace("@localhost:", "@127.0.0.1:")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()

# Far enough ahead that no row left behind by another test file — they all
# carry real timestamps — can fall inside a window under test. Combined with
# never committing, that makes every count below exactly what this test seeded.
NOW = datetime(2031, 6, 15, 12, 0, tzinfo=UTC)


def _seed_sale(
    session,
    *,
    paid_at: datetime,
    status: str = OrderStatus.PAID,
    unit_price: str = "2.50",
    unit_cost: str = "1.00",
    provider: str = "esimaccess",
    with_esim: bool = True,
    expires_at: datetime | None = None,
    esim_status: str = ESIMStatus.ACTIVE,
) -> Order:
    country = Country(
        name=f"Testland {uuid.uuid4().hex[:6]}",
        slug=f"testland-{uuid.uuid4().hex[:8]}",
        iso2=uuid.uuid4().hex[:2].upper(),
    )
    session.add(country)
    session.flush()

    plan = Plan(
        country_id=country.id,
        title="Testland 3 GB · 15 days",
        data_amount_mb=3072,
        validity_days=15,
        price_usd=Decimal(unit_price),
        cost_usd=Decimal(unit_cost),
        provider=provider,
    )
    session.add(plan)

    customer = Customer(
        email=f"report-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Report",
        created_at=paid_at,
    )
    session.add(customer)
    session.flush()

    order = Order(
        customer_id=customer.id,
        status=status,
        subtotal=Decimal(unit_price),
        discount=Decimal("0"),
        total=Decimal(unit_price),
        amount_uzs=Decimal("29999"),
        exchange_rate=Decimal("11915.64"),
        created_at=paid_at,
        paid_at=paid_at if status == OrderStatus.PAID else None,
    )
    session.add(order)
    session.flush()

    session.add(
        OrderItem(
            order_id=order.id,
            plan_id=plan.id,
            unit_price=Decimal(unit_price),
            unit_cost=Decimal(unit_cost),
            quantity=1,
        )
    )
    if with_esim:
        session.add(
            ESIM(
                order_id=order.id,
                plan_id=plan.id,
                customer_id=customer.id,
                iccid=uuid.uuid4().hex[:19],
                qr_payload="LPA:1$example$CODE",
                provider=provider,
                provider_esim_tran_no="tran-1",
                status=esim_status,
                data_total_mb=3072,
                validity_days=15,
                created_at=paid_at,
                expires_at=expires_at,
            )
        )
    session.flush()
    return order


async def test_revenue_cost_and_margin_come_from_the_frozen_lines() -> None:
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=2))
        _seed_sale(session, paid_at=NOW - timedelta(hours=5), provider="esimcard")

        report = build_report(session, days=1, now=NOW)

    assert report.orders == 2
    assert report.revenue_usd == Decimal("5.00")
    assert report.cost_usd == Decimal("2.00")
    assert report.margin_usd == Decimal("3.00")
    assert report.margin_percent == Decimal("150")
    assert report.revenue_uzs == Decimal("59998.00")


async def test_each_supplier_is_reported_separately() -> None:
    """The owner buys from two wholesalers and wants each one's share."""
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=1), provider="esimaccess")
        _seed_sale(session, paid_at=NOW - timedelta(hours=2), provider="esimaccess")
        _seed_sale(session, paid_at=NOW - timedelta(hours=3), provider="esimcard")

        report = build_report(session, days=1, now=NOW)

    by_provider = {line.provider: line for line in report.suppliers}
    assert by_provider["esimaccess"].esims == 2
    assert by_provider["esimcard"].esims == 1
    assert by_provider["esimaccess"].cost_usd == Decimal("2.00")


async def test_an_unpaid_order_earns_nothing() -> None:
    """A checkout that was opened and abandoned is not revenue."""
    with worker_session() as session:
        _seed_sale(
            session,
            paid_at=NOW - timedelta(hours=1),
            status=OrderStatus.PENDING,
            with_esim=False,
        )

        report = build_report(session, days=1, now=NOW)

    assert report.orders == 0
    assert report.revenue_usd == Decimal("0.00")
    assert report.abandoned_checkouts == 1


async def test_a_sale_outside_the_window_is_not_counted() -> None:
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(days=3))

        day = build_report(session, days=1, now=NOW)
        week = build_report(session, days=7, now=NOW)

    assert day.orders == 0
    assert week.orders == 1


async def test_a_paid_order_with_no_esim_is_reported_as_a_problem() -> None:
    """The failure worth waking up for: charged, and nothing delivered."""
    with worker_session() as session:
        order = _seed_sale(session, paid_at=NOW - timedelta(hours=1), with_esim=False)

        report = build_report(session, days=1, now=NOW)

    assert report.unfulfilled_orders == [order.id]
    assert "eSIM berilmadi" in format_report(report)


async def test_each_purchase_is_listed_with_its_tariff_country_and_supplier() -> None:
    with worker_session() as session:
        _seed_sale(
            session,
            paid_at=NOW - timedelta(hours=1),
            provider="esimcard",
            expires_at=NOW + timedelta(days=14),
        )

        report = build_report(session, days=1, now=NOW)

    assert len(report.purchases) == 1
    line = report.purchases[0]
    assert line.plan == "Testland 3 GB · 15 days"
    assert line.provider == "esimcard"
    assert line.country.startswith("Testland")
    assert line.expires_at is not None


async def test_an_esim_that_ran_out_in_the_window_is_reported() -> None:
    """The owner asked to be told when a plan finishes."""
    with worker_session() as session:
        _seed_sale(
            session,
            paid_at=NOW - timedelta(hours=20),
            expires_at=NOW - timedelta(hours=1),
            esim_status=ESIMStatus.EXPIRED,
        )

        report = build_report(session, days=1, now=NOW)

    assert len(report.expired) == 1
    assert "MUDDATI TUGAGANLAR" in format_report(report)


async def test_the_message_fits_in_one_telegram_send() -> None:
    """Forty sales must not push the problems section past the 4096 limit."""
    with worker_session() as session:
        for hour in range(40):
            _seed_sale(session, paid_at=NOW - timedelta(minutes=hour + 1))

        report = build_report(session, days=1, now=NOW)

    text = format_report(report)
    assert len(text) <= 4096, len(text)
    assert "MUAMMOLAR" in text
    assert "va yana" in text


async def test_the_summary_leads_with_people_and_money() -> None:
    """The two figures asked for first: who bought, and how much came in."""
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=1))
        _seed_sale(session, paid_at=NOW - timedelta(hours=2))

        report = build_report(session, days=1, now=NOW)

    assert report.buying_customers == 2
    text = format_report(report)
    assert "2 odamga sotildi" in text
    assert "59 998 so'm" in text


async def test_volume_sold_is_reported_in_gigabytes() -> None:
    """Two 3 GB plans are 6 GB, and it is written the way a customer reads it."""
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=1))
        _seed_sale(session, paid_at=NOW - timedelta(hours=2))

        report = build_report(session, days=1, now=NOW)

    assert report.total_data_mb == 6144
    assert "Jami hajm: <b>6 GB</b>" in format_report(report)


async def test_an_unfulfilled_order_sold_no_gigabytes() -> None:
    """Volume follows the eSIMs issued, not the money taken.

    An order that charged and delivered nothing must not appear in the report as
    data sold — that is the one number that would hide the failure.
    """
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=1), with_esim=False)

        report = build_report(session, days=1, now=NOW)

    assert report.orders == 1
    assert report.total_data_mb == 0


async def test_each_purchase_shows_its_size_and_price() -> None:
    with worker_session() as session:
        _seed_sale(session, paid_at=NOW - timedelta(hours=1))

        text = format_report(build_report(session, days=1, now=NOW))

    assert "3 GB / 15 kun" in text
    assert "29 999 so'm" in text
