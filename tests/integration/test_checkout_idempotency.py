"""What an `Idempotency-Key` may and may not replay.

The key stops one cart becoming two paid orders. It must not also stop a
customer buying the same thing twice, and that is the line these tests draw —
because the storefront derives the key from the cart itself, so "the same cart
again" and "the same request again" arrive looking identical.

The case that motivated the file: somebody buys a Turkey 5 GB, pays, then buys a
second one the same evening for whoever they are travelling with. Same cart,
same key. Replaying the first order's payment link there hands them a link to an
order that is already paid, buys nothing, and — with the storefront's
confirmation step — reports success, because that order really is settled.
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.cache import get_redis
from app.core.config import settings
from app.db.models import Order
from app.db.models.enums import OrderStatus
from app.db.session import SessionFactory
from app.main import app
from app.services.orders import _idempotency_key

KEY = "5x1|"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _a_provider_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payme, because it needs no outbound call to build a payment link."""
    monkeypatch.setattr(settings, "payment_provider", "payme")
    monkeypatch.setattr(settings, "payme_merchant_id", settings.payme_merchant_id or "ci-merchant")


@pytest.fixture
async def buyer(client: AsyncClient) -> dict:
    email = f"idem-{uuid.uuid4().hex[:10]}@example.com"
    registered = await client.post(
        "/api/auth/register",
        json={"email": email, "full_name": "Idem", "password": "correct horse battery"},
    )
    if registered.status_code == 429:
        pytest.skip("register rate limit reached")
    assert registered.status_code == 201, registered.text

    countries = (await client.get("/api/countries?limit=1")).json()
    if not countries:
        pytest.skip("catalogue is empty; run scripts.seed")
    detail = (await client.get(f"/api/countries/{countries[0]['slug']}")).json()
    if not detail.get("plans"):
        pytest.skip("catalogue has no sellable plan")
    return {
        "headers": {"Authorization": f"Bearer {registered.json()['access_token']}"},
        "plan_id": detail["plans"][0]["id"],
    }


async def place(client: AsyncClient, buyer: dict, key: str = KEY) -> dict:
    response = await client.post(
        "/api/checkout",
        json={"items": [{"plan_id": buyer["plan_id"], "quantity": 1}]},
        headers={**buyer["headers"], "Idempotency-Key": key},
    )
    if response.status_code == 503:
        pytest.skip("exchange rate unavailable")
    assert response.status_code == 201, response.text
    return response.json()


async def set_status(order_id: int, status: str) -> None:
    async with SessionFactory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.status = status
        await session.commit()


class TestTheKeyStillStopsADoubleOrder:
    async def test_the_same_request_twice_is_one_order(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        """The whole point of the key: a retried tap must not open a second order."""
        first = await place(client, buyer)
        second = await place(client, buyer)
        assert second["order_id"] == first["order_id"]
        assert second["payment_url"] == first["payment_url"]

    async def test_a_different_cart_is_a_different_order(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        first = await place(client, buyer, key="5x1|")
        second = await place(client, buyer, key="5x2|")
        assert second["order_id"] != first["order_id"]

    async def test_no_key_means_no_replay(self, client: AsyncClient, buyer: dict) -> None:
        """A client that sends nothing gets the old behaviour, not a shared order."""
        first = await client.post(
            "/api/checkout",
            json={"items": [{"plan_id": buyer["plan_id"], "quantity": 1}]},
            headers=buyer["headers"],
        )
        second = await client.post(
            "/api/checkout",
            json={"items": [{"plan_id": buyer["plan_id"], "quantity": 1}]},
            headers=buyer["headers"],
        )
        if first.status_code == 503:
            pytest.skip("exchange rate unavailable")
        assert first.json()["order_id"] != second.json()["order_id"]


class TestTheKeyDoesNotOutliveItsOrder:
    async def test_buying_the_same_cart_again_after_paying_opens_a_new_order(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        """The bug this file exists for.

        Two identical eSIMs bought the same day is an ordinary thing to want.
        Before the fix the second attempt replayed the first order — already
        paid — so nothing was bought and the storefront called it settled.
        """
        first = await place(client, buyer)
        await set_status(first["order_id"], OrderStatus.PAID)

        second = await place(client, buyer)

        assert second["order_id"] != first["order_id"], (
            "a paid order was replayed; the customer would have bought nothing"
        )
        async with SessionFactory() as session:
            fresh = await session.get(Order, second["order_id"])
            assert fresh is not None
            assert fresh.status == OrderStatus.PENDING

    async def test_a_cancelled_order_is_not_replayed(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        """Cancelling is the escape hatch out of a stale payment link.

        Without this, a one-line cart had no way back: removing the only item
        and adding it again rebuilds the very same key.
        """
        first = await place(client, buyer)
        cancelled = await client.post(
            f"/api/checkout/{first['order_id']}/cancel", headers=buyer["headers"]
        )
        assert cancelled.status_code == 204, cancelled.text

        second = await place(client, buyer)
        assert second["order_id"] != first["order_id"]

    async def test_a_pending_order_is_still_replayed(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        """The guard reads the order's state, not the clock."""
        first = await place(client, buyer)
        await set_status(first["order_id"], OrderStatus.PENDING)
        assert (await place(client, buyer))["order_id"] == first["order_id"]

    async def test_an_order_that_no_longer_exists_is_not_replayed(
        self, client: AsyncClient, buyer: dict
    ) -> None:
        """Redis outliving the row it names is the one case with no order to read.

        Reached by rewriting the cached answer rather than deleting an order:
        orders are never deleted here, and the branch still has to hold when a
        database is restored from a backup taken before the cache was written.
        """
        first = await place(client, buyer)
        async with SessionFactory() as session:
            order = await session.get(Order, first["order_id"])
            assert order is not None
            customer_id = order.customer_id

        cache_key = _idempotency_key(customer_id, KEY)
        stored = json.loads(await get_redis().get(cache_key))
        stored["order_id"] = 2_000_000_000
        await get_redis().set(cache_key, json.dumps(stored))

        second = await place(client, buyer)
        assert second["order_id"] != 2_000_000_000
