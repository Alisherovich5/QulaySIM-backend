"""eSIM lifecycle and ownership rules against a real database."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.core.security import hash_password
from app.db.models import ESIM, Customer, Order, Plan
from app.db.models.enums import ESIMStatus
from app.db.session import SessionFactory
from app.main import app
from app.services import account as service


async def _make_customer(session, *, password: str = "a-long-enough-password") -> Customer:
    customer = Customer(
        email=f"acct-{uuid.uuid4().hex[:12]}@example.com",
        full_name="Lifecycle Test",
        hashed_password=hash_password(password),
    )
    session.add(customer)
    await session.flush()
    return customer


async def _make_esim(session, customer: Customer, **overrides) -> ESIM:
    from sqlalchemy import select

    plan = (await session.execute(select(Plan).limit(1))).scalars().first()
    assert plan is not None, "seed the catalogue first: python -m scripts.seed"

    order = Order(customer_id=customer.id, status="paid", total=Decimal("9.99"))
    session.add(order)
    await session.flush()

    fields = {
        "order_id": order.id,
        "plan_id": plan.id,
        "customer_id": customer.id,
        "iccid": uuid.uuid4().hex[:20],
        "qr_payload": "LPA:1$example.com$TOKEN",
        "status": ESIMStatus.PENDING,
        "data_total_mb": 1024,
        "validity_days": 7,
    }
    fields.update(overrides)
    esim = ESIM(**fields)
    session.add(esim)
    await session.flush()
    return esim


@pytest.fixture
async def session():
    async with SessionFactory() as s:
        yield s
        await s.rollback()


class TestActivation:
    async def test_activation_sets_window(self, session) -> None:
        customer = await _make_customer(session)
        esim = await _make_esim(session, customer)

        activated = await service.activate_esim(session, customer, esim.id)

        assert activated.status == ESIMStatus.ACTIVE
        assert activated.activated_at is not None
        assert activated.expires_at is not None
        delta = activated.expires_at - activated.activated_at
        assert delta == timedelta(days=7)

    async def test_activation_is_idempotent(self, session) -> None:
        """A double click must not push the expiry date out a second time."""
        customer = await _make_customer(session)
        esim = await _make_esim(session, customer)

        first = await service.activate_esim(session, customer, esim.id)
        first_expiry = first.expires_at

        second = await service.activate_esim(session, customer, esim.id)
        assert second.expires_at == first_expiry

    async def test_expired_esim_cannot_be_activated(self, session) -> None:
        customer = await _make_customer(session)
        esim = await _make_esim(session, customer, status=ESIMStatus.EXPIRED)

        with pytest.raises(ConflictError):
            await service.activate_esim(session, customer, esim.id)

    async def test_cannot_activate_someone_elses_esim(self, session) -> None:
        """Ownership is enforced in the query, so another customer's eSIM is
        simply not found rather than being activated."""
        owner = await _make_customer(session)
        attacker = await _make_customer(session)
        esim = await _make_esim(session, owner)

        with pytest.raises(NotFoundError):
            await service.activate_esim(session, attacker, esim.id)


class TestTopUpIsGone:
    """Topping up was free and fictional, so the route no longer exists.

    The old tests asserted that pressing it raised the allowance — which it did,
    without charging anyone and without telling the supplier, so the customer
    ended up holding a number they could not spend. They passed on behaviour
    that should never have shipped, which is why they are replaced rather than
    fixed. What is asserted now is the absence: no endpoint, and no service
    function behind it for a future caller to rediscover.
    """

    async def test_the_endpoint_is_not_routed(self) -> None:
        # Routers are included rather than flattened, so `app.routes` holds
        # wrappers without a `path`; the generated schema is the reliable list
        # of what is actually reachable.
        paths = set(app.openapi()["paths"])
        assert not any(path.endswith("/topup") for path in paths), sorted(paths)

    async def test_the_service_no_longer_offers_it(self) -> None:
        assert not hasattr(service, "topup_esim")


class TestProfile:
    async def test_rename(self, session) -> None:
        customer = await _make_customer(session)
        updated = await service.update_profile(
            session, customer, full_name="  New Name  ", current_password=None, new_password=None
        )
        assert updated.full_name == "New Name"

    async def test_password_change_requires_current_password(self, session) -> None:
        customer = await _make_customer(session)
        with pytest.raises(DomainError, match="current password"):
            await service.update_profile(
                session,
                customer,
                full_name=None,
                current_password=None,
                new_password="brand-new-password",
            )

    async def test_wrong_current_password_rejected(self, session) -> None:
        customer = await _make_customer(session)
        with pytest.raises(PermissionDeniedError):
            await service.update_profile(
                session,
                customer,
                full_name=None,
                current_password="not-the-password",
                new_password="brand-new-password",
            )

    async def test_password_change_succeeds(self, session) -> None:
        from app.core.security import verify_password

        customer = await _make_customer(session, password="original-password-x")
        await service.update_profile(
            session,
            customer,
            full_name=None,
            current_password="original-password-x",
            new_password="replacement-password",
        )
        assert verify_password("replacement-password", customer.hashed_password)


class TestReviewEligibility:
    async def test_customer_without_esim_cannot_review(self, session) -> None:
        customer = await _make_customer(session)
        with pytest.raises(PermissionDeniedError):
            await service.submit_testimonial(
                session, customer, rating=5, location="Tashkent", text="x" * 20
            )

    async def test_status_reports_ineligible(self, session) -> None:
        customer = await _make_customer(session)
        status = await service.testimonial_status(session, customer)
        assert status["eligible"] is False
        assert status["status"] is None


class TestSummary:
    async def test_counts_reflect_owned_esims(self, session) -> None:
        customer = await _make_customer(session)
        await _make_esim(session, customer, status=ESIMStatus.ACTIVE, data_used_mb=512)
        await _make_esim(session, customer, status=ESIMStatus.PENDING)
        await session.commit()

        result = await service.summary(session, customer)
        assert result["total_esims"] == 2
        assert result["active_esims"] == 1
        assert result["data_used_mb"] == 512
        assert result["email"] == customer.email

    async def test_passport_names_follow_the_language(self, session) -> None:
        """The passport used to select Country.name directly, so the account page
        said "Turkey" while the destinations list on the same site said
        "Turkiya"."""
        from sqlalchemy import select

        from app.db.models import Country, Plan

        customer = await _make_customer(session)
        esim = await _make_esim(session, customer, status=ESIMStatus.ACTIVE)
        plan = (await session.execute(select(Plan).where(Plan.id == esim.plan_id))).scalar_one()
        country = (
            await session.execute(select(Country).where(Country.id == plan.country_id))
        ).scalar_one()
        english = country.name
        original = (country.name_uz, country.name_ru)
        country.name_uz = "Sinov mamlakati"
        # Cleared on purpose: an untranslated country must fall back to the base
        # name, not to blank.
        country.name_ru = ""
        # Flushed, not committed: this suite runs against the development
        # database, so a committed rename would outlive the test and show up in
        # the admin and on the site.
        await session.flush()
        try:
            uzbek = await service.summary(session, customer, language="uz")
            assert uzbek["passport"][0]["name"] == "Sinov mamlakati"

            russian = await service.summary(session, customer, language="ru")
            assert russian["passport"][0]["name"] == english
        finally:
            country.name_uz, country.name_ru = original
            await session.flush()


class TestReferralCode:
    async def test_code_is_allocated_lazily(self, session) -> None:
        customer = await _make_customer(session)
        assert customer.referral_code is None

        code = await service.ensure_referral_code(session, customer)
        assert len(code) == 8

        # Stable across calls — a customer's code must not change.
        assert await service.ensure_referral_code(session, customer) == code
