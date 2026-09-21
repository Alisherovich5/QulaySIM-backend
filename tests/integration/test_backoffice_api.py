"""The backoffice API, from outside.

The first test in this file is the one that matters most: it walks every route
the app declares under /api/v1/backoffice and asserts that an anonymous caller
gets 401. It is written against the route table rather than a list, so an
endpoint added next month without a `CurrentStaff` dependency fails here on the
day it is written instead of serving customer emails to the internet.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models import (
    ESIM,
    Country,
    Customer,
    Order,
    OrderItem,
    Plan,
    Staff,
    SupportTicket,
)
from app.db.session import SessionFactory
from app.main import app
from app.services.backoffice import auth as service

pytestmark = pytest.mark.anyio

PREFIX = "/api/v1/backoffice"
# These four are the way IN — they cannot require a token to be called.
PUBLIC = {f"{PREFIX}/auth/login", f"{PREFIX}/auth/refresh", f"{PREFIX}/auth/logout"}

PASSWORD = "Backoffice-test-2026"
# Produced by Django's own hasher at a low cost factor: the live format with
# 1,000,000 iterations costs a second per check, which would add minutes to the
# suite for no extra confidence in this code path.
ENCODED = "pbkdf2_sha256$100$P4l1UVIA8vxfAwHzB84bBP$cqPhrfPv+qz7uyhwWmnw/QHfDOM7mLcbC7mcmQihiLI="


def _routes() -> list[tuple[str, str]]:
    """Every backoffice route the app declares, read off the OpenAPI schema.

    Not `app.routes`: included routers appear there as opaque holders, so
    walking that list finds nothing and this test would pass while checking
    zero endpoints.
    """
    out: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith(PREFIX) or path in PUBLIC:
            continue
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            out.append((method.upper(), path))
    return sorted(out)


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _staff(*, superuser: bool = True) -> Staff:
    async with SessionFactory() as session:
        person = Staff(
            username=f"bo-{uuid.uuid4().hex[:10]}",
            email=f"bo-{uuid.uuid4().hex[:8]}@qulaysim.uz",
            password=ENCODED,
            is_staff=True,
            is_active=True,
            is_superuser=superuser,
            first_name="Test",
            last_name="Operator",
        )
        session.add(person)
        await session.commit()
        await session.refresh(person)
        return person


def _token(person: Staff) -> str:
    from app.core.security import create_access_token

    return create_access_token(service.subject(person))


class TestNothingIsOpen:
    async def test_every_route_refuses_an_anonymous_caller(self) -> None:
        routes = _routes()
        assert len(routes) > 25, "the route table looks empty — this test would pass vacuously"

        leaked: list[str] = []
        async with await _client() as client:
            for method, path in routes:
                url = path.format(
                    order_id=1,
                    esim_id=1,
                    customer_id=1,
                    ticket_id=1,
                    recipient_id=1,
                    provider="esimcard",
                )
                response = await client.request(method, url, json={})
                if response.status_code != 401:
                    leaked.append(f"{method} {path} -> {response.status_code}")
        assert not leaked, f"reachable without signing in: {leaked}"

    async def test_a_customer_token_is_not_a_staff_token(self) -> None:
        """Both families are signed with the same key. The subject is what
        keeps them apart, so a customer's own valid access token must not open
        the backoffice."""
        from app.core.security import create_access_token

        customer_token = create_access_token("1")
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/dashboard", headers={"Authorization": f"Bearer {customer_token}"}
            )
        assert response.status_code == 401

    async def test_a_staff_token_is_not_a_customer_token(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                "/api/account/orders", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 401


class TestSigningIn:
    async def test_the_right_password_opens_a_session(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": PASSWORD}
            )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]
        # The credential is httpOnly; the hint beside it is not and carries nothing.
        assert "qs_bo_refresh" in response.cookies
        assert response.cookies.get("qs_bo") == "1"

    async def test_the_username_works_as_well_as_the_email(self) -> None:
        # The box says "email" because that is what people know; the unique
        # column is the username, and an operator typing either must get in.
        person = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.username, "password": PASSWORD}
            )
        assert response.status_code == 200

    async def test_a_wrong_password_is_refused(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": "wrong-one"}
            )
        assert response.status_code == 401
        assert "qs_bo_refresh" not in response.cookies

    async def test_an_unknown_account_is_refused_the_same_way(self) -> None:
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
            )
        assert response.status_code == 401

    async def test_a_non_staff_account_cannot_sign_in(self) -> None:
        person = await _staff()
        async with SessionFactory() as session:
            row = await session.get(Staff, person.id)
            assert row is not None
            row.is_staff = False
            await session.commit()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": PASSWORD}
            )
        assert response.status_code == 401

    async def test_me_names_the_person_who_signed_in(self) -> None:
        person = await _staff(superuser=False)
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Test Operator"
        assert body["role"] == "operator"


class TestPermissions:
    async def test_an_operator_cannot_switch_tariffs(self) -> None:
        person = await _staff(superuser=False)
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/plans/active",
                headers={"Authorization": f"Bearer {_token(person)}"},
                json={"ids": [1], "is_active": False},
            )
        assert response.status_code == 403

    async def test_the_owner_can(self) -> None:
        person = await _staff(superuser=True)
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/plans/active",
                headers={"Authorization": f"Bearer {_token(person)}"},
                json={"ids": [-1], "is_active": False},
            )
        assert response.status_code == 204


class TestTheDataItServes:
    async def test_the_dashboard_answers(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/dashboard", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"attention", "wallets", "today", "catalogue", "expiring"}

    async def test_a_paid_order_with_no_esim_is_what_needs_attention(self) -> None:
        from datetime import UTC, datetime, timedelta

        person = await _staff()
        async with SessionFactory() as session:
            customer = Customer(email=f"att-{uuid.uuid4().hex[:8]}@example.com", full_name="Late")
            session.add(customer)
            await session.flush()
            plan = (await session.execute(select(Plan).limit(1))).scalars().first()
            assert plan is not None, "seed the catalogue first"
            order = Order(
                customer_id=customer.id,
                status="paid",
                total=Decimal("10"),
                amount_uzs=Decimal("120000"),
                paid_at=datetime.now(UTC) - timedelta(hours=1),
            )
            session.add(order)
            await session.flush()
            session.add(
                OrderItem(order_id=order.id, plan_id=plan.id, unit_price=Decimal("10"), quantity=1)
            )
            await session.commit()
            order_id = order.id

        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/dashboard", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 200
        assert order_id in [row["id"] for row in response.json()["attention"]]

        # …and once the eSIM exists it drops off the list.
        async with SessionFactory() as session:
            order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
            plan_id = (
                await session.execute(
                    select(OrderItem.plan_id).where(OrderItem.order_id == order_id)
                )
            ).scalar_one()
            session.add(
                ESIM(
                    order_id=order_id,
                    plan_id=plan_id,
                    customer_id=order.customer_id,
                    iccid=uuid.uuid4().hex[:20],
                    qr_payload="LPA:1$x$y",
                    status="active",
                )
            )
            await session.commit()

        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/dashboard", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert order_id not in [row["id"] for row in response.json()["attention"]]

    async def test_search_finds_a_customer_by_a_fragment_of_their_address(self) -> None:
        person = await _staff()
        marker = uuid.uuid4().hex[:10]
        async with SessionFactory() as session:
            session.add(Customer(email=f"{marker}@example.com", full_name="Findable"))
            await session.commit()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/search?q={marker[:6]}",
                headers={"Authorization": f"Bearer {_token(person)}"},
            )
        assert response.status_code == 200
        assert any(marker in row["email"] for row in response.json()["customers"])

    async def test_a_one_character_search_returns_nothing_rather_than_everything(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/search?q=a", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        body = response.json()
        assert body == {"customers": [], "orders": [], "esims": []}

    async def test_grantable_plans_are_scoped_to_the_country_asked_for(self) -> None:
        person = await _staff()
        async with SessionFactory() as session:
            country = (
                (
                    await session.execute(
                        select(Country).join(Plan, Plan.country_id == Country.id).limit(1)
                    )
                )
                .scalars()
                .first()
            )
            assert country is not None, "seed the catalogue first"
            iso2 = country.iso2
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/plans/grantable?davlat={iso2}",
                headers={"Authorization": f"Bearer {_token(person)}"},
            )
        assert response.status_code == 200
        assert all(row["country_iso2"].upper() == iso2.upper() for row in response.json())

    async def test_an_unknown_country_gives_an_empty_list_not_an_error(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/plans/grantable?davlat=ZZ",
                headers={"Authorization": f"Bearer {_token(person)}"},
            )
        assert response.status_code == 200
        assert response.json() == []


class TestTickets:
    async def test_the_contact_form_becomes_a_ticket(self) -> None:
        """The reason the table exists: before this, a support message that
        arrived while nobody was watching Telegram was simply gone."""
        person = await _staff()
        marker = uuid.uuid4().hex[:10]
        async with await _client() as client:
            posted = await client.post(
                "/api/support/message",
                json={
                    "name": "Dilnur",
                    "email": f"{marker}@example.com",
                    "phone": "+998 90 123 45 67",
                    "locale": "uz",
                    "message": "eSIM o‘rnatilmayapti, yordam bering.",
                },
            )
            assert posted.status_code in (202, 503), posted.text

            listed = await client.get(
                f"{PREFIX}/tickets", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert listed.status_code == 200
        rows = [
            row
            for row in listed.json()["items"]
            if row["customer_email"] == f"{marker}@example.com"
        ]
        assert len(rows) == 1
        assert rows[0]["state"] == "new"
        assert rows[0]["subject"].startswith("eSIM o‘rnatilmayapti")

    async def test_a_note_moves_it_out_of_new_and_closing_ends_it(self) -> None:
        person = await _staff()
        async with SessionFactory() as session:
            ticket = SupportTicket(
                name="X", email=f"t-{uuid.uuid4().hex[:8]}@example.com", message="Salom"
            )
            session.add(ticket)
            await session.commit()
            ticket_id = ticket.id

        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            assert (
                await client.post(
                    f"{PREFIX}/tickets/{ticket_id}/reply",
                    headers=headers,
                    json={"body": "Telefonda javob berdim"},
                )
            ).status_code == 201
            detail = await client.get(f"{PREFIX}/tickets/{ticket_id}", headers=headers)
            assert detail.json()["state"] == "answered"
            assert [m["from"] for m in detail.json()["messages"]] == ["customer", "staff"]

            assert (
                await client.post(f"{PREFIX}/tickets/{ticket_id}/close", headers=headers)
            ).status_code == 204
            closed = await client.get(f"{PREFIX}/tickets/{ticket_id}", headers=headers)
            assert closed.json()["state"] == "closed"


class TestGrants:
    async def test_a_grant_to_an_unknown_address_opens_an_account_and_queues_fulfilment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: list[int] = []

        from app.workers.tasks import provisioning

        monkeypatch.setattr(
            provisioning.fulfil_paid_order, "delay", lambda order_id: queued.append(order_id)
        )

        person = await _staff()
        async with SessionFactory() as session:
            plan = (
                (
                    await session.execute(
                        select(Plan).where(Plan.is_active.is_(True), Plan.provider != "mock")
                    )
                )
                .scalars()
                .first()
            )
            if plan is None:
                pytest.skip("no real-supplier plan in the seeded catalogue")
            plan_id, cost = plan.id, Decimal(str(plan.cost_usd or 0))

        email = f"grant-{uuid.uuid4().hex[:10]}@example.com"
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/grants",
                headers={"Authorization": f"Bearer {_token(person)}"},
                json={"email": email, "plan_id": plan_id, "reason": None},
            )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["customer_created"] is True
        assert queued == [body["order_id"]]

        async with SessionFactory() as session:
            order = (
                await session.execute(select(Order).where(Order.id == body["order_id"]))
            ).scalar_one()
            # Free, but it still cost us — and the reports read exactly this.
            assert order.is_complimentary is True
            assert order.status == "paid"
            assert order.amount_uzs == Decimal("0.00")
            item = (
                await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
            ).scalar_one()
            assert item.unit_price == Decimal("0")
            assert item.unit_cost == cost

    async def test_a_mock_plan_is_refused_rather_than_handed_over(self) -> None:
        person = await _staff()
        async with SessionFactory() as session:
            plan = (
                (await session.execute(select(Plan).where(Plan.provider == "mock")))
                .scalars()
                .first()
            )
            if plan is None:
                pytest.skip("no mock plan in the seeded catalogue")
            plan_id = plan.id
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/grants",
                headers={"Authorization": f"Bearer {_token(person)}"},
                json={"email": "someone@example.com", "plan_id": plan_id},
            )
        assert response.status_code == 409

    async def test_a_reason_is_optional(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/grants",
                headers={"Authorization": f"Bearer {_token(person)}"},
                json={"email": "x@example.com", "plan_id": -1},
            )
        # Refused for the missing plan, NOT for the missing reason: a 422 here
        # would mean the field is still required.
        assert response.status_code == 404


class TestTwoFactor:
    async def test_an_enrolled_account_is_asked_for_a_code_not_refused(self) -> None:
        """428, not 401. The password was right; answering "wrong credentials"
        would tell somebody holding a correct password that it was wrong, and
        the login form would never show the code field."""
        from app.db.models import TOTPDevice

        person = await _staff()
        async with SessionFactory() as session:
            session.add(
                TOTPDevice(
                    user_id=person.id,
                    name="phone",
                    confirmed=True,
                    key=b"12345678901234567890".hex(),
                    step=30,
                    t0=0,
                    digits=6,
                    tolerance=1,
                    drift=0,
                    last_t=-1,
                )
            )
            await session.commit()

        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": PASSWORD}
            )
        assert response.status_code == 428
        assert "qs_bo_refresh" not in response.cookies

    async def test_a_wrong_code_does_not_open_a_session(self) -> None:
        from app.db.models import TOTPDevice

        person = await _staff()
        async with SessionFactory() as session:
            session.add(
                TOTPDevice(
                    user_id=person.id,
                    name="phone",
                    confirmed=True,
                    key=b"12345678901234567890".hex(),
                    step=30,
                    t0=0,
                    digits=6,
                    tolerance=1,
                    drift=0,
                    last_t=-1,
                )
            )
            await session.commit()

        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login",
                json={"email": person.email, "password": PASSWORD, "code": "000000"},
            )
        assert response.status_code == 401
        assert "qs_bo_refresh" not in response.cookies

    async def test_an_unconfirmed_device_does_not_lock_anybody_out(self) -> None:
        """django-otp creates the row before the person proves they can read
        it. Treating that as "2FA is on" would lock out an operator who started
        enrolling and stopped."""
        from app.db.models import TOTPDevice

        person = await _staff()
        async with SessionFactory() as session:
            session.add(
                TOTPDevice(
                    user_id=person.id,
                    name="half-enrolled",
                    confirmed=False,
                    key=b"12345678901234567890".hex(),
                    step=30,
                    t0=0,
                    digits=6,
                    tolerance=1,
                    drift=0,
                    last_t=-1,
                )
            )
            await session.commit()

        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": PASSWORD}
            )
        assert response.status_code == 200
