"""The backoffice API, from outside.

The first test in this file is the one that matters most: it walks every route
the app declares under /api/v1/backoffice and asserts that an anonymous caller
gets 401. It is written against the route table rather than a list, so an
endpoint added next month without a `CurrentStaff` dependency fails here on the
day it is written instead of serving customer emails to the internet.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
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
                # Any placeholder, filled with any value: the auth dependency
                # runs before the route body, so what the id points at never
                # matters. A regex rather than a keyword list means an endpoint
                # added next month with a parameter nobody listed is still
                # checked instead of raising KeyError here.
                url = re.sub(r"\{[^}]+\}", "1", path)
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


class TestTheShapesTheScreensRead:
    """Field-by-field checks on the responses a page would crash without.

    Both of these were found by loading the deployed app in a browser, not by
    any test: the API answered 200 with a body missing one key, and the page
    died on `undefined.total`. A 200 is not a passing contract.
    """

    async def test_the_orders_list_carries_counts_for_the_whole_table(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/orders?size=1", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "total", "page", "pages", "counts"}
        assert {"total", "pending", "delivered", "failed"} <= set(body["counts"])
        # Whole-table, not this page: the tiles say how much work there is, and
        # a count of the fifty rows on screen answers a different question.
        assert body["counts"]["total"] == body["total"]

    async def test_a_telegram_recipient_says_only_what_the_table_holds(self) -> None:
        person = await _staff()
        async with await _client() as client:
            response = await client.get(
                f"{PREFIX}/telegram", headers={"Authorization": f"Bearer {_token(person)}"}
            )
        assert response.status_code == 200
        for row in response.json()["items"]:
            assert set(row) == {"id", "chat_id", "label", "is_active"}


class TestFiltersActuallyFilter:
    """A query parameter the endpoint does not declare is silently dropped, and
    the answer is a 200 with an unfiltered list. Every one of these was spelled
    differently on the two sides — `state` against `holat`, `status` against
    `holat` — and every one of them looked like it worked.
    """

    async def test_an_order_stage_filter_narrows_the_list(self) -> None:
        person = await _staff()
        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            everything = await client.get(f"{PREFIX}/orders?size=200", headers=headers)
            delivered = await client.get(
                f"{PREFIX}/orders?size=200&stage=delivered", headers=headers
            )
        assert delivered.status_code == 200
        rows = delivered.json()["items"]
        assert all(row["stage"] == "delivered" for row in rows)
        # And it is a real narrowing, not an empty list that trivially passes.
        stages = {row["stage"] for row in everything.json()["items"]}
        if len(stages) > 1:
            assert len(rows) < len(everything.json()["items"])

    async def test_an_esim_status_filter_narrows_the_list(self) -> None:
        person = await _staff()
        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            response = await client.get(f"{PREFIX}/esims?size=200&status=expired", headers=headers)
        assert response.status_code == 200
        assert all(row["status"] == "expired" for row in response.json()["items"])

    async def test_a_ticket_state_filter_narrows_the_list(self) -> None:
        person = await _staff()
        async with SessionFactory() as session:
            session.add(SupportTicket(email="open@example.com", message="ochiq", state="new"))
            session.add(SupportTicket(email="shut@example.com", message="yopiq", state="closed"))
            await session.commit()

        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            new = await client.get(f"{PREFIX}/tickets?state=new", headers=headers)
            closed = await client.get(f"{PREFIX}/tickets?state=closed", headers=headers)
        assert {row["state"] for row in new.json()["items"]} == {"new"}
        assert {row["state"] for row in closed.json()["items"]} == {"closed"}


async def _all_orders(client: AsyncClient, headers: dict[str, str], stage: str) -> list[dict]:
    """Every page of one stage. The endpoint caps a page at 200 on purpose, so
    a test that asks for 500 gets a 422 and a confusing KeyError."""
    out: list[dict] = []
    page = 1
    while True:
        body = (
            await client.get(f"{PREFIX}/orders?size=200&page={page}&stage={stage}", headers=headers)
        ).json()
        out.extend(body["items"])
        if page >= body["pages"]:
            return out
        page += 1


class TestTheTilesAgreeWithTheList:
    """The count above a list and the list itself must be derived from one
    rule. They were not: top-ups leave no eSIM row, so a fully applied one was
    delivered in the list and not in the tile — 47 against 44, with nothing on
    screen to explain the gap.
    """

    async def test_delivered_counts_the_same_rows_the_filter_returns(self) -> None:
        person = await _staff()
        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            counts = (await client.get(f"{PREFIX}/orders?size=1", headers=headers)).json()["counts"]
            rows = await _all_orders(client, headers, "delivered")
        assert counts["delivered"] == len(rows)

    async def test_every_stage_adds_up_to_the_total(self) -> None:
        person = await _staff()
        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            body = (await client.get(f"{PREFIX}/orders?size=1", headers=headers)).json()
        counts = body["counts"]
        assert counts["pending"] + counts["delivered"] + counts["failed"] <= counts["total"], (
            "more orders counted than exist"
        )
        # `paid and in flight` is the remainder and has no tile of its own.
        assert counts["total"] == body["total"]

    async def test_a_supplier_refusal_makes_the_order_read_as_failed(self) -> None:
        """A paid order the supplier refused used to read `paid` — in flight,
        nothing to do — while the customer had no eSIM and nobody was told."""
        from datetime import UTC, datetime

        from app.db.models import SupplierPurchase

        person = await _staff()
        async with SessionFactory() as session:
            customer = Customer(
                email=f"ref-{uuid.uuid4().hex[:8]}@example.com", full_name="Refused"
            )
            session.add(customer)
            await session.flush()
            plan = (await session.execute(select(Plan).limit(1))).scalars().first()
            assert plan is not None, "seed the catalogue first"
            order = Order(
                customer_id=customer.id,
                status="paid",
                total=Decimal("10"),
                amount_uzs=Decimal("120000"),
                paid_at=datetime.now(UTC),
            )
            session.add(order)
            await session.flush()
            session.add(
                OrderItem(order_id=order.id, plan_id=plan.id, unit_price=Decimal("10"), quantity=1)
            )
            session.add(
                SupplierPurchase(
                    order_id=order.id,
                    provider="esimcard",
                    line_key=uuid.uuid4().hex[:12],
                    state="failed",
                    note="Insufficient Wallet Balance",
                )
            )
            await session.commit()
            order_id = order.id

        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            rows = await _all_orders(client, headers, "failed")
        row = next((r for r in rows if r["id"] == order_id), None)
        assert row is not None, "a refused order is not on the failed list"
        assert row["failure"] == "Insufficient Wallet Balance"


class TestTheSessionIsActuallyShort:
    async def test_the_refresh_token_expires_with_its_cookie(self) -> None:
        """Twelve hours, both of them.

        The cookie said Max-Age=43200 while the token inside it was signed for
        thirty days — the storefront's default. A browser would have dropped it
        after twelve hours; anybody who captured it would not have.
        """
        import jwt

        from app.core.config import settings

        person = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/auth/login", json={"email": person.email, "password": PASSWORD}
            )
        token = response.cookies["qs_bo_refresh"]
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.service_name,
        )
        lifetime = payload["exp"] - payload["iat"]
        assert lifetime == 12 * 3600, f"refresh token lives {lifetime / 3600:.0f}h, not 12h"


class TestWhatTheDjangoAdminUsedToDo:
    """The five screens that had no interface at all after Django was retired.

    Each one was reachable in the old admin and nowhere else, so the test that
    matters for each is the same: the list answers, and the action that changes
    something actually changes it.
    """

    async def test_a_promo_code_can_be_switched_off_but_only_by_the_owner(self) -> None:
        from app.db.models import PromoCode

        async with SessionFactory() as session:
            code = PromoCode(
                code=f"TEST{uuid.uuid4().hex[:8].upper()}",
                discount_type="percent",
                discount_value=Decimal("10"),
                is_active=True,
            )
            session.add(code)
            await session.commit()
            promo_id, text = code.id, code.code

        operator = await _staff(superuser=False)
        owner = await _staff(superuser=True)
        async with await _client() as client:
            # Reading is support work; an operator may do it.
            listed = await client.get(
                f"{PREFIX}/promocodes", headers={"Authorization": f"Bearer {_token(operator)}"}
            )
            assert listed.status_code == 200
            assert any(row["code"] == text for row in listed.json()["items"])

            refused = await client.patch(
                f"{PREFIX}/promocodes/{promo_id}",
                headers={"Authorization": f"Bearer {_token(operator)}"},
                json={"is_active": False},
            )
            assert refused.status_code == 403, "an operator switched off a discount code"

            allowed = await client.patch(
                f"{PREFIX}/promocodes/{promo_id}",
                headers={"Authorization": f"Bearer {_token(owner)}"},
                json={"is_active": False},
            )
            assert allowed.status_code == 204

        async with SessionFactory() as session:
            assert (await session.get(PromoCode, promo_id)).is_active is False

    async def test_a_duplicate_promo_code_is_refused(self) -> None:
        owner = await _staff()
        headers = {"Authorization": f"Bearer {_token(owner)}"}
        code = f"DUP{uuid.uuid4().hex[:8].upper()}"
        async with await _client() as client:
            first = await client.post(
                f"{PREFIX}/promocodes", headers=headers, json={"code": code, "discount_value": 5}
            )
            assert first.status_code == 201
            second = await client.post(
                f"{PREFIX}/promocodes",
                headers=headers,
                json={"code": code.lower(), "discount_value": 5},
            )
        # Lower case, same code. Two rows would mean the second one silently
        # never applies — checkout matches on one of them.
        assert second.status_code == 409

    async def test_a_percentage_over_a_hundred_is_refused(self) -> None:
        owner = await _staff()
        async with await _client() as client:
            response = await client.post(
                f"{PREFIX}/promocodes",
                headers={"Authorization": f"Bearer {_token(owner)}"},
                json={
                    "code": f"FREE{uuid.uuid4().hex[:6]}",
                    "discount_type": "percent",
                    "discount_value": 150,
                },
            )
        assert response.status_code == 409, "a 150% discount would pay the customer"

    async def test_a_review_is_invisible_until_somebody_approves_it(self) -> None:
        from app.db.models import Testimonial

        async with SessionFactory() as session:
            review = Testimonial(
                name="Dilnur",
                location="Toshkent",
                text="Yaxshi ishladi",
                rating=5,
                moderation_status="pending",
                is_active=False,
            )
            session.add(review)
            await session.commit()
            review_id = review.id

        person = await _staff()
        headers = {"Authorization": f"Bearer {_token(person)}"}
        async with await _client() as client:
            pending = await client.get(f"{PREFIX}/reviews?state=pending", headers=headers)
            assert review_id in [row["id"] for row in pending.json()["items"]]

            done = await client.post(
                f"{PREFIX}/reviews/{review_id}/moderate",
                headers=headers,
                json={"state": "approved"},
            )
            assert done.status_code == 204

        async with SessionFactory() as session:
            row = await session.get(Testimonial, review_id)
            assert row.moderation_status == "approved"
            # Approved AND on. Two switches for one decision is how a review
            # ends up approved and still missing from the site.
            assert row.is_active is True

    async def test_a_pricing_rule_is_owner_only_and_records_who_changed_it(self) -> None:
        from app.db.models import PricingRule

        async with SessionFactory() as session:
            # Provider-scoped, not global: the table carries a partial unique
            # index allowing exactly one global rule, and a test that creates a
            # second one fails on the constraint rather than on the code.
            rule = PricingRule(
                scope="provider",
                provider=f"test-{uuid.uuid4().hex[:8]}",
                markup_percent=Decimal("30"),
                min_margin_usd=Decimal("0"),
                rounding="charm",
                is_active=True,
                updated_at=datetime.now(UTC),
            )
            session.add(rule)
            await session.commit()
            rule_id = rule.id

        operator = await _staff(superuser=False)
        owner = await _staff(superuser=True)
        async with await _client() as client:
            refused = await client.patch(
                f"{PREFIX}/pricing-rules/{rule_id}",
                headers={"Authorization": f"Bearer {_token(operator)}"},
                json={"markup_percent": 5},
            )
            assert refused.status_code == 403, "an operator changed the shop's margin"

            allowed = await client.patch(
                f"{PREFIX}/pricing-rules/{rule_id}",
                headers={"Authorization": f"Bearer {_token(owner)}"},
                json={"markup_percent": 42.5, "note": "sinov"},
            )
            assert allowed.status_code == 204

        async with SessionFactory() as session:
            row = await session.get(PricingRule, rule_id)
            assert row.markup_percent == Decimal("42.50")
            assert row.note == "sinov"

    async def test_an_offer_can_be_taken_out_of_the_running(self) -> None:
        from app.db.models import SupplierOffer

        async with SessionFactory() as session:
            # A plan that has no offer yet: the table allows one offer per
            # plan per supplier, so picking the first plan in the catalogue
            # collides with whatever the importer already wrote.
            taken = select(SupplierOffer.plan_id).where(SupplierOffer.provider == "esimcard")
            plan = (
                (await session.execute(select(Plan).where(Plan.id.not_in(taken)).limit(1)))
                .scalars()
                .first()
            )
            if plan is None:
                pytest.skip("every plan already carries an eSIMCard offer")
            offer = SupplierOffer(
                plan_id=plan.id,
                provider="esimcard",
                package_code=uuid.uuid4().hex[:16],
                cost_usd=Decimal("3.20"),
                is_available=True,
                updated_at=datetime.now(UTC),
            )
            session.add(offer)
            await session.commit()
            offer_id = offer.id

        owner = await _staff()
        headers = {"Authorization": f"Bearer {_token(owner)}"}
        async with await _client() as client:
            off = await client.patch(
                f"{PREFIX}/offers/{offer_id}",
                headers=headers,
                json={"is_available": False, "reason": "doim rad etadi"},
            )
            assert off.status_code == 204

            listed = await client.get(f"{PREFIX}/offers?holat=unavailable", headers=headers)
            row = next((r for r in listed.json()["items"] if r["id"] == offer_id), None)
            assert row is not None
            assert row["unavailable_reason"] == "doim rad etadi"

            # And back on — the reason must not survive, or the row reads as
            # unavailable for a reason while being available.
            on = await client.patch(
                f"{PREFIX}/offers/{offer_id}", headers=headers, json={"is_available": True}
            )
            assert on.status_code == 204

        async with SessionFactory() as session:
            row2 = await session.get(SupplierOffer, offer_id)
            assert row2.is_available is True
            assert row2.unavailable_reason == ""

    async def test_referrals_name_both_sides_and_summarise_the_agents(self) -> None:
        from app.db.models import Referral

        async with SessionFactory() as session:
            agent = Customer(email=f"agent-{uuid.uuid4().hex[:8]}@example.com", full_name="Agent")
            invited = Customer(email=f"inv-{uuid.uuid4().hex[:8]}@example.com", full_name="Invited")
            session.add_all([agent, invited])
            await session.flush()
            session.add(
                Referral(
                    referrer_id=agent.id,
                    referred_id=invited.id,
                    referred_email=invited.email,
                    status="completed",
                    reward_code="X",
                )
            )
            await session.commit()
            agent_email, invited_email = agent.email, invited.email

        person = await _staff()
        async with await _client() as client:
            body = (
                await client.get(
                    # Searched rather than listed: the development database
                    # carries a thousand agents from earlier runs, and any
                    # fixed page size makes this test about the page size.
                    f"{PREFIX}/referrals?q={agent_email}",
                    headers={"Authorization": f"Bearer {_token(person)}"},
                )
            ).json()
        row = next((r for r in body["items"] if r["referrer_email"] == agent_email), None)
        assert row is not None
        assert row["referred_email"] == invited_email
        agent = next((a for a in body["agents"] if a["email"] == agent_email), None)
        assert agent is not None, "the customer who made the invitation is not in the agent list"
        assert agent["invited"] >= 1
        assert agent["completed"] >= 1
