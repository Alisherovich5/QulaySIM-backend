"""The money path, end to end against a real database.

Payme retries every method, so the assertions here are mostly about what
happens the *second* time a call arrives.
"""

from __future__ import annotations

import base64
import json
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.services.currency import charm_uzs

RPC = "/api/payments/payme"


def auth_header(key: str | None = None) -> dict[str, str]:
    token = key if key is not None else settings.payme_test_key
    return {"Authorization": "Basic " + base64.b64encode(f"Paycom:{token}".encode()).decode()}


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def rpc(client: AsyncClient, method: str, params: dict, **kw) -> dict:
    headers = kw.pop("headers", auth_header())
    response = await client.post(
        RPC, content=json.dumps({"id": 1, "method": method, "params": params}), headers=headers
    )
    assert response.status_code == 200, "Payme reads the JSON-RPC error, not the HTTP status"
    return response.json()


@pytest.fixture
async def paid_order(client: AsyncClient) -> dict:
    """A pending order with its som amount frozen."""
    if settings.payment_provider != "payme" or not settings.payme_merchant_id:
        pytest.skip("Payme is not configured in this environment")

    email = f"payme-{uuid.uuid4().hex[:10]}@example.com"
    registered = await client.post(
        "/api/auth/register",
        json={"email": email, "full_name": "Payme", "password": "correct horse battery"},
    )
    if registered.status_code == 429:
        pytest.skip("register rate limit reached")
    token = registered.json()["access_token"]

    countries = (await client.get("/api/countries?limit=1")).json()
    if not countries:
        pytest.skip("catalogue is empty; run scripts.seed")
    detail = (await client.get(f"/api/countries/{countries[0]['slug']}")).json()

    placed = await client.post(
        "/api/checkout",
        json={"items": [{"plan_id": detail["plans"][0]["id"], "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    if placed.status_code == 503:
        pytest.skip("exchange rate unavailable")
    assert placed.status_code == 201, placed.text
    body = placed.json()
    body["tiyin"] = int(Decimal(str(body["amount_uzs"])) * 100)
    return body


class TestAuthorisation:
    async def test_no_header_is_refused(self, client: AsyncClient) -> None:
        body = await rpc(client, "CheckPerformTransaction", {}, headers={})
        assert body["error"]["code"] == -32504

    async def test_wrong_key_is_refused(self, client: AsyncClient) -> None:
        body = await rpc(client, "CheckPerformTransaction", {}, headers=auth_header("wrong"))
        assert body["error"]["code"] == -32504


class TestCheckPerform:
    async def test_allows_a_correct_request(self, client: AsyncClient, paid_order: dict) -> None:
        body = await rpc(
            client,
            "CheckPerformTransaction",
            {"amount": paid_order["tiyin"], "account": {"order_id": str(paid_order["order_id"])}},
        )
        assert body["result"] == {"allow": True}

    @pytest.mark.parametrize("delta", [-1, 1, -100000, 100000])
    async def test_rejects_a_mismatched_amount(
        self, client: AsyncClient, paid_order: dict, delta: int
    ) -> None:
        """The frozen amount is the contract; anything else must not be charged."""
        body = await rpc(
            client,
            "CheckPerformTransaction",
            {
                "amount": paid_order["tiyin"] + delta,
                "account": {"order_id": str(paid_order["order_id"])},
            },
        )
        assert body["error"]["code"] == -31001

    async def test_rejects_an_unknown_order(self, client: AsyncClient) -> None:
        body = await rpc(
            client, "CheckPerformTransaction", {"amount": 1000, "account": {"order_id": "99999999"}}
        )
        assert body["error"]["code"] == -31050

    async def test_rejects_a_missing_account(self, client: AsyncClient) -> None:
        body = await rpc(client, "CheckPerformTransaction", {"amount": 1000, "account": {}})
        assert body["error"]["code"] == -31050


class TestTransactionLifecycle:
    async def test_create_perform_and_replay(self, client: AsyncClient, paid_order: dict) -> None:
        account = {"order_id": str(paid_order["order_id"])}
        tx = uuid.uuid4().hex[:24]
        params = {"id": tx, "time": 1, "amount": paid_order["tiyin"], "account": account}

        created = await rpc(client, "CreateTransaction", params)
        assert created["result"]["state"] == 1

        # Payme retries; the same transaction must come back, not a new one.
        again = await rpc(client, "CreateTransaction", params)
        assert again["result"]["transaction"] == created["result"]["transaction"]
        assert again["result"]["create_time"] == created["result"]["create_time"]

        # A different transaction on the same order would be a double charge.
        second = await rpc(
            client,
            "CreateTransaction",
            {
                "id": uuid.uuid4().hex[:24],
                "time": 1,
                "amount": paid_order["tiyin"],
                "account": account,
            },
        )
        assert second["error"]["code"] == -31051

        performed = await rpc(client, "PerformTransaction", {"id": tx})
        assert performed["result"]["state"] == 2

        replayed = await rpc(client, "PerformTransaction", {"id": tx})
        assert replayed["result"]["perform_time"] == performed["result"]["perform_time"], (
            "a replay must not move the perform time"
        )

        checked = await rpc(client, "CheckTransaction", {"id": tx})
        assert checked["result"]["state"] == 2

        # Cancelling a performed transaction is a refund: state -2, not -1.
        cancelled = await rpc(client, "CancelTransaction", {"id": tx, "reason": 5})
        assert cancelled["result"]["state"] == -2

        recancelled = await rpc(client, "CancelTransaction", {"id": tx, "reason": 5})
        assert recancelled["result"]["cancel_time"] == cancelled["result"]["cancel_time"]

    async def test_cancelling_before_perform_gives_minus_one(
        self, client: AsyncClient, paid_order: dict
    ) -> None:
        tx = uuid.uuid4().hex[:24]
        await rpc(
            client,
            "CreateTransaction",
            {
                "id": tx,
                "time": 1,
                "amount": paid_order["tiyin"],
                "account": {"order_id": str(paid_order["order_id"])},
            },
        )
        cancelled = await rpc(client, "CancelTransaction", {"id": tx, "reason": 1})
        assert cancelled["result"]["state"] == -1

    async def test_unknown_transaction(self, client: AsyncClient) -> None:
        for method in ("PerformTransaction", "CancelTransaction", "CheckTransaction"):
            body = await rpc(client, method, {"id": "does-not-exist"})
            assert body["error"]["code"] == -31003, method


class TestOtherMethods:
    async def test_get_statement_returns_a_list(self, client: AsyncClient) -> None:
        body = await rpc(client, "GetStatement", {"from": 0, "to": 99_999_999_999_999})
        assert isinstance(body["result"]["transactions"], list)

    async def test_change_password_is_refused(self, client: AsyncClient) -> None:
        assert (await rpc(client, "ChangePassword", {"password": "x"}))["error"]["code"] == -32400

    async def test_unknown_method(self, client: AsyncClient) -> None:
        assert (await rpc(client, "Nonsense", {}))["error"]["code"] == -32601

    async def test_malformed_body_does_not_crash(self, client: AsyncClient) -> None:
        response = await client.post(RPC, content=b"not json", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32601


class TestOrderPlacement:
    async def test_placement_freezes_the_som_amount(self, paid_order: dict) -> None:
        assert paid_order["amount_uzs"] > 0
        assert paid_order["exchange_rate"] > 0
        converted = Decimal(
            str(round(paid_order["total_usd"] * paid_order["exchange_rate"], 2))
        )
        # Not the raw conversion: the amount is rounded down to the figure the
        # storefront showed while the customer was choosing. Asserting equality
        # with `charm_uzs` rather than a hand-written number keeps this test
        # honest if the ladder is ever retuned, while the rule's own cases are
        # pinned in tests/unit/test_charm_pricing.py.
        assert Decimal(str(paid_order["amount_uzs"])) == charm_uzs(converted)

    async def test_placement_bills_a_999_amount_close_to_the_conversion(
        self, paid_order: dict
    ) -> None:
        """What is charged ends in 999 and stays within half a thousand of USD×rate.

        The rounding goes both ways, so this cannot assert an upper bound of the
        raw conversion. What it can assert is that the invoice carries the same
        shape the customer was shown, and that the gap is the rounding and not a
        pricing bug.
        """
        converted = paid_order["total_usd"] * paid_order["exchange_rate"]
        assert paid_order["amount_uzs"] % 1_000 == 999
        assert abs(paid_order["amount_uzs"] - converted) <= 500

    async def test_payment_url_carries_the_frozen_amount(self, paid_order: dict) -> None:
        decoded = base64.b64decode(paid_order["payment_url"].rsplit("/", 1)[1]).decode()
        assert f"ac.order_id={paid_order['order_id']}" in decoded
        assert f"a={paid_order['tiyin']}" in decoded

    async def test_placing_an_order_requires_a_login(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/checkout", json={"items": [{"plan_id": 1, "quantity": 1}]}
        )
        assert response.status_code == 401
