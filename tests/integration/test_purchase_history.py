"""What a customer's purchase history shows, and what it must not.

Two things this file exists to hold down, both found in production rather than
here:

  * `/account/orders` embeds eSIMs, and eSIMs report what they were paid for by
    reading their order line. Eager-loading was set up on the eSIM listing but
    not on the order listing, so serialising the response tried a lazy load
    outside the async greenlet and the endpoint answered 500. A response schema
    that reaches through a relationship needs the query to agree with it, and
    nothing but a test that calls the endpoint will say so.
  * An order that was never paid is not a purchase. Listing it as
    "Order #41 · 0 eSIMs · awaiting" made an abandoned checkout tab look like an
    outstanding debt.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import ESIM, Customer, Order, OrderItem, Plan
from app.db.session import SessionFactory
from app.main import app

pytestmark = pytest.mark.anyio


async def _seed(status: str) -> tuple[Customer, Order]:
    async with SessionFactory() as session:
        customer = Customer(
            email=f"history-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password=hash_password("History-2026-pass"),
            full_name="History",
        )
        session.add(customer)
        await session.flush()

        plan = (await session.execute(Plan.__table__.select().limit(1))).first()
        assert plan is not None, "catalogue fixture is required"
        plan_id = plan.id

        order = Order(
            customer_id=customer.id,
            status=status,
            subtotal=Decimal("1.00"),
            discount=Decimal("0"),
            total=Decimal("1.00"),
            amount_uzs=Decimal("11999"),
            exchange_rate=Decimal("11915.64"),
        )
        session.add(order)
        await session.flush()

        session.add(
            OrderItem(order_id=order.id, plan_id=plan_id, unit_price=Decimal("1.00"), quantity=1)
        )
        session.add(
            ESIM(
                order_id=order.id,
                plan_id=plan_id,
                customer_id=customer.id,
                iccid=uuid.uuid4().hex[:19],
                qr_payload="LPA:1$example$CODE",
                data_total_mb=1024,
                validity_days=7,
            )
        )
        await session.commit()
        await session.refresh(customer)
        await session.refresh(order)
        return customer, order


@asynccontextmanager
async def _client_for(customer: Customer):
    """A signed-in client. A context manager so the transport is always closed."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login",
            data={"username": customer.email, "password": "History-2026-pass"},
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client


async def test_a_paid_order_serialises_with_its_esim_prices() -> None:
    """The regression: this answered 500, not a wrong number."""
    customer, _ = await _seed("paid")
    async with _client_for(customer) as client:
        response = await client.get("/api/account/orders")

    assert response.status_code == 200, response.text
    orders = response.json()
    assert len(orders) == 1
    esim = orders[0]["esims"][0]
    assert Decimal(str(esim["paid_usd"])) == Decimal("1.00")
    # 1.00 x 11915.64 = 11915.64, and the charm rule lands it on 11 999 —
    # the same figure the checkout froze onto the order.
    assert Decimal(str(esim["paid_uzs"])) == Decimal("11999")


async def test_the_esim_listing_carries_the_price_too() -> None:
    customer, _ = await _seed("paid")
    async with _client_for(customer) as client:
        response = await client.get("/api/account/esims")

    assert response.status_code == 200, response.text
    esim = response.json()[0]
    assert Decimal(str(esim["paid_usd"])) == Decimal("1.00")
    assert Decimal(str(esim["paid_uzs"])) == Decimal("11999")


@pytest.mark.parametrize("status", ["pending", "cancelled"])
async def test_an_unpaid_order_is_not_history(status: str) -> None:
    customer, _ = await _seed(status)
    async with _client_for(customer) as client:
        response = await client.get("/api/account/orders")

    assert response.status_code == 200, response.text
    assert response.json() == []
