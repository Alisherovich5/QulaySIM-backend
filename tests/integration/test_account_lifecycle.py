"""eSIM lifecycle and ownership rules against a real database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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


class TestTopUpIsPaidFor:
    """Topping up exists again — as a purchase this time.

    The first version raised the allowance for free: no charge, no supplier call,
    so the customer held a number they could not spend. Those tests were deleted
    along with the feature, and what replaced them was an assertion of absence.

    Absence is no longer the invariant; *payment* is. A top-up now goes through an
    order, a payment provider and the wholesaler, exactly like a first purchase.
    So these assert the two things that must never come back: no route that grants
    data directly, and no service function that adds megabytes without a supplier
    confirming it.
    """

    async def test_the_account_router_cannot_grant_data(self) -> None:
        """Listing what is available is fine; adding data is not.

        `/account/esims/{id}/topups` is a read. The only writer is the checkout
        path, which cannot finish without a payment.
        """
        paths = set(app.openapi()["paths"])
        granting = [
            path for path in paths if path.startswith("/api/account") and path.endswith("/topup")
        ]
        assert granting == [], sorted(paths)

    async def test_the_account_service_no_longer_offers_it(self) -> None:
        assert not hasattr(service, "topup_esim")

    async def test_the_purchase_path_needs_a_signed_in_customer(self) -> None:
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/api/checkout/topup", json={"esim_id": 1, "package_code": "TOPUP_X"}
            )
        assert response.status_code == 401

    async def test_data_is_only_added_after_the_supplier_confirms(self) -> None:
        """The one ordering rule in fulfilment.

        `_apply_topups` calls the wholesaler and only then writes the new
        allowance — the reverse would tell a customer they have 5 GB more when
        nothing was bought, and a retry can fix a missing update but cannot take
        back a promise.
        """
        import inspect

        from app.workers.tasks import provisioning

        source = inspect.getsource(provisioning._apply_topups)
        assert source.index("client.topup(") < source.index("esim.data_total_mb =")


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


class TestReferralSummary:
    """What an agent opens the page to find out.

    The money is the part worth guarding: an agent is paid per invitee who
    actually pays, and a page that counted sign-ups instead would promise money
    nobody owes. Stavka pog'onali bo'lgani uchun yana bitta shart qo'shildi --
    olib kelingan mijozning haqi keyinchalik o'zgarmaydi.
    """

    async def _referral(
        self,
        session,
        referrer,
        *,
        status: str,
        name: str = "",
        paid_uzs: str | None = "150000",
        completed_at: datetime | None = None,
    ) -> None:
        from app.db.models import Order, Referral

        invitee = None
        if name:
            invitee = Customer(
                email=f"inv-{uuid.uuid4().hex[:10]}@example.com",
                full_name=name,
                hashed_password=hash_password("a-long-enough-password"),
            )
            session.add(invitee)
            await session.flush()

            if status == "completed" and paid_uzs is not None:
                session.add(
                    Order(
                        customer_id=invitee.id,
                        status="paid",
                        amount_uzs=Decimal(paid_uzs),
                    )
                )
                await session.flush()

        session.add(
            Referral(
                referrer_id=referrer.id,
                referred_id=invitee.id if invitee else None,
                referred_email=invitee.email if invitee else "waiting@example.com",
                status=status,
                reward_code="RWD-1" if status == "completed" else "",
                completed_at=completed_at or (datetime.now(UTC) if status == "completed" else None),
            )
        )
        await session.flush()

    async def test_pays_only_for_invitees_who_bought(self, session, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "0:5%")
        referrer = await _make_customer(session)
        await self._referral(session, referrer, status="completed", name="Sotib Olgan")
        await self._referral(session, referrer, status="completed", name="Yana Bittasi")
        await self._referral(session, referrer, status="pending", name="Hali Olmagan")

        data = await service.referral_summary(session, referrer)

        assert data["invited"] == 3
        assert data["completed"] == 2
        assert data["pending"] == 1
        # 150 000 dan 5% -- Dilnur yozgan misolning o'zi.
        assert data["earned_uzs"] == 15_000
        assert data["rate"]["label"] == "5%"

    async def test_the_rate_rises_at_the_threshold(self, session, monkeypatch) -> None:
        """Uchinchi mijozdan boshlab stavka ko'tariladi, oldingilari o'z joyida
        qoladi. Aks holda allaqachon aytilgan summa keyin o'zgarib ketardi."""

        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "0:5000,2:6000")
        referrer = await _make_customer(session)
        for index in range(3):
            await self._referral(
                session,
                referrer,
                status="completed",
                name=f"Mijoz {index}",
                completed_at=datetime.now(UTC) + timedelta(minutes=index),
            )

        data = await service.referral_summary(session, referrer)

        assert data["earned_uzs"] == 5000 + 5000 + 6000
        assert [e["commission_uzs"] for e in data["entries"] if e["status"] == "completed"] == [
            6000,
            5000,
            5000,
        ]

    async def test_shows_what_the_next_threshold_is_worth(self, session, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "0:5%,100:6%")
        referrer = await _make_customer(session)
        await self._referral(session, referrer, status="completed", name="Birinchi")

        data = await service.referral_summary(session, referrer)

        assert data["next_rate"] == {
            "label": "6%",
            "percent": 6.0,
            "flat_uzs": None,
            "at": 100,
            "needed": 99,
        }

    async def test_no_next_threshold_at_the_top(self, session, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "0:5%")
        referrer = await _make_customer(session)

        data = await service.referral_summary(session, referrer)

        assert data["next_rate"] is None

    async def test_a_broken_setting_does_not_break_the_page(self, session, monkeypatch) -> None:
        """Kabinetning boshqa bo'limlari ham shu javobga bog'liq."""

        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "yuz foiz")
        monkeypatch.setattr(settings, "referral_commission_uzs", 6000)
        referrer = await _make_customer(session)
        await self._referral(session, referrer, status="completed", name="Sotib Olgan")

        data = await service.referral_summary(session, referrer)

        assert data["earned_uzs"] == 6000

    async def test_an_order_without_a_som_amount_pays_nothing_by_percent(
        self, session, monkeypatch
    ) -> None:
        """Eski buyurtmalarda so'm ustuni bo'sh. Foizni yo'qdan hisoblab
        bo'lmaydi -- to'qib chiqarilgan summadan ko'ra nol xavfsizroq."""

        from app.core.config import settings

        monkeypatch.setattr(settings, "referral_commission_tiers", "0:5%")
        referrer = await _make_customer(session)
        await self._referral(
            session, referrer, status="completed", name="Eski Mijoz", paid_uzs=None
        )

        data = await service.referral_summary(session, referrer)

        assert data["completed"] == 1
        assert data["earned_uzs"] == 0

    async def test_lists_the_invitee_by_name(self, session) -> None:
        referrer = await _make_customer(session)
        await self._referral(session, referrer, status="completed", name="Dilnur Ibadullayev")

        data = await service.referral_summary(session, referrer)
        entry = data["entries"][0]

        assert entry["referred_name"] == "Dilnur Ibadullayev"
        assert entry["referred_email"]
        assert entry["status"] == "completed"

    async def test_an_invitation_nobody_accepted_is_still_listed(self, session) -> None:
        """The outer join matters: a pending row has no customer to join to, and
        dropping it would make the invited count disagree with the list."""

        referrer = await _make_customer(session)
        await self._referral(session, referrer, status="pending")

        data = await service.referral_summary(session, referrer)

        assert data["invited"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["referred_name"] == ""
        assert data["earned_uzs"] == 0

    async def test_nothing_earned_before_anyone_joins(self, session) -> None:
        referrer = await _make_customer(session)
        data = await service.referral_summary(session, referrer)

        assert data["invited"] == 0
        assert data["earned_uzs"] == 0
        assert data["code"]
