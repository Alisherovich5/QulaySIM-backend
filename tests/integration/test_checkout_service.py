"""Cart pricing against the real catalogue."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.db.models import Plan
from app.db.session import SessionFactory
from app.schemas.commerce import CartItemIn
from app.services import checkout as service


@pytest.fixture
async def session():
    async with SessionFactory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def plan(session) -> Plan:
    row = (
        (await session.execute(select(Plan).where(Plan.is_active.is_(True)).limit(1)))
        .scalars()
        .first()
    )
    assert row is not None, "seed the catalogue first: python -m scripts.seed"
    return row


class TestPriceCart:
    async def test_single_line(self, session, plan: Plan) -> None:
        quote = await service.price_cart(session, [CartItemIn(plan_id=plan.id, quantity=1)], None)
        assert quote.subtotal == plan.price_usd
        assert quote.total == plan.price_usd

    async def test_quantity_multiplies(self, session, plan: Plan) -> None:
        quote = await service.price_cart(session, [CartItemIn(plan_id=plan.id, quantity=3)], None)
        assert quote.subtotal == plan.price_usd * 3

    async def test_unknown_plan_rejected(self, session) -> None:
        with pytest.raises(DomainError, match="unavailable"):
            await service.price_cart(session, [CartItemIn(plan_id=99_999_999, quantity=1)], None)

    async def test_inactive_plan_rejected(self, session, plan: Plan) -> None:
        plan.is_active = False
        await session.flush()
        with pytest.raises(DomainError, match="unavailable"):
            await service.price_cart(session, [CartItemIn(plan_id=plan.id, quantity=1)], None)

    async def test_unknown_promo_reports_reason_without_failing(self, session, plan: Plan) -> None:
        quote = await service.price_cart(
            session, [CartItemIn(plan_id=plan.id, quantity=1)], "NO-SUCH-CODE"
        )
        assert quote.promo_applied is False
        assert quote.promo_message == "Promo code is invalid"
        assert quote.total == plan.price_usd

    async def test_whole_cart_loads_in_one_query(self, session, plan: Plan) -> None:
        """Regression guard: pricing used to issue one SELECT per line item."""
        from sqlalchemy import event

        statements: list[str] = []

        def record(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        sync_engine = session.get_bind()
        event.listen(sync_engine, "before_cursor_execute", record)
        try:
            await service.price_cart(
                session,
                [CartItemIn(plan_id=plan.id, quantity=1) for _ in range(1)]
                + [CartItemIn(plan_id=plan.id + 1, quantity=2)],
                None,
            )
        finally:
            event.remove(sync_engine, "before_cursor_execute", record)

        plan_selects = [s for s in statements if "catalog_plan" in s]
        assert len(plan_selects) == 1, f"expected one plan query, got {len(plan_selects)}"


class TestCheckoutGate:
    """Placing an order must be impossible unless a provider is actually
    configured — otherwise a customer gets an order they can never pay for."""

    async def test_disabled_provider_is_refused(self, session, plan: Plan, monkeypatch) -> None:
        from app.core.config import settings
        from app.core.errors import ServiceUnavailableError
        from app.db.models import Customer
        from app.services import orders as order_service

        monkeypatch.setattr(settings, "payment_provider", "disabled")
        customer = Customer(email="gate@example.com", hashed_password="x")

        with pytest.raises(ServiceUnavailableError):
            await order_service.place_order(
                session, customer, [CartItemIn(plan_id=plan.id, quantity=1)], None
            )

    async def test_payme_without_a_merchant_id_is_refused(
        self, session, plan: Plan, monkeypatch
    ) -> None:
        """A blank merchant id would produce a checkout link that 404s."""
        from app.core.config import settings
        from app.core.errors import ServiceUnavailableError
        from app.db.models import Customer
        from app.services import orders as order_service

        monkeypatch.setattr(settings, "payment_provider", "payme")
        monkeypatch.setattr(settings, "payme_merchant_id", "")
        customer = Customer(email="gate2@example.com", hashed_password="x")

        with pytest.raises(ServiceUnavailableError):
            await order_service.place_order(
                session, customer, [CartItemIn(plan_id=plan.id, quantity=1)], None
            )
