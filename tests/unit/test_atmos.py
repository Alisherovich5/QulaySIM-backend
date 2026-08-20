"""ATMOS — the pieces that can be proven without a database.

The callback flow itself lives in tests/integration/test_atmos_callback.py;
here are the primitives it stands on: the signature formula, the source-range
check, the token cache, and the invoice call's contract handling.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.core.config import settings
from app.integrations import atmos as client
from app.services import atmos as service


@pytest.fixture(autouse=True)
def _atmos_settings(monkeypatch):
    monkeypatch.setattr(settings, "atmos_consumer_key", "ck")
    monkeypatch.setattr(settings, "atmos_consumer_secret", "cs")
    monkeypatch.setattr(settings, "atmos_store_id", 77)
    monkeypatch.setattr(settings, "atmos_callback_api_key", "api-key-x")
    monkeypatch.setattr(settings, "atmos_ikpu_code", "10305002001000000")
    client._clear_token()


class TestSignature:
    def test_the_documented_concatenation(self):
        raw = "77tx-1421100000api-key-x"
        # md5 is ATMOS's protocol digest, not our choice of security primitive.
        assert service.expected_sign("77", "tx-142", "1", "100000") == (
            hashlib.md5(raw.encode()).hexdigest()  # noqa: S324
        )

    def test_values_are_hashed_as_received_not_normalised(self):
        # "100000" and "100000.0" are the same number but different signatures;
        # normalising before hashing would break verification for whichever
        # form ATMOS actually sent.
        assert service.expected_sign("77", "t", "1", "100000") != service.expected_sign(
            "77", "t", "1", "100000.0"
        )


class TestCallerRange:
    def test_the_documented_range_is_allowed(self):
        assert service.caller_allowed("92.63.207.5")

    def test_everything_else_is_not(self):
        assert not service.caller_allowed("8.8.8.8")
        assert not service.caller_allowed("not-an-ip")
        assert not service.caller_allowed("")


class TestTokenLifecycle:
    def _transport(self, journal):
        def handler(request: httpx.Request) -> httpx.Response:
            journal.append((request.method, request.url.path))
            if request.url.path == "/token":
                return httpx.Response(
                    200, json={"access_token": f"tok-{len(journal)}", "expires_in": 3600}
                )
            auth = request.headers.get("Authorization", "")
            # First bearer is refused once, to prove the refresh-and-retry.
            if auth == "Bearer tok-1":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={"url": "https://checkout.atmos.uz/invoice?id=abc", "status": {"code": "0"}},
            )

        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_token_is_cached_and_refreshed_once_on_401(self):
        journal: list[tuple[str, str]] = []
        url = await client.create_invoice(
            account="1",
            amount_tiyin=100000,
            lines=[{"name": "Turkey 3GB", "amount_tiyin": 100000, "quantity": 1}],
            transport=self._transport(journal),
        )
        assert url == "https://checkout.atmos.uz/invoice?id=abc"
        paths = [p for _, p in journal]
        # token → refused create → fresh token → successful create
        assert paths == ["/token", "/checkout/invoice/create", "/token", "/checkout/invoice/create"]

    @pytest.mark.asyncio
    async def test_a_urlless_answer_is_an_error_not_a_blank_redirect(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"access_token": "t", "expires_in": 3600}
                if request.url.path == "/token"
                else {"status": {"code": "406", "description": "invalid ikpu"}},
            )
        )
        with pytest.raises(client.AtmosError):
            await client.create_invoice(
                account="1",
                amount_tiyin=100000,
                lines=[{"name": "x", "amount_tiyin": 100000, "quantity": 1}],
                transport=transport,
            )


@pytest.mark.asyncio
async def test_items_always_carry_the_required_details_object(monkeypatch):
    # `details` missing is what produced -999999 on the DEV store, and it must
    # be an object rather than the array the doc's example shows. Sent even
    # without a fiscal code, since `code` is the optional half.
    monkeypatch.setattr(settings, "atmos_ikpu_code", "")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"url": "https://x/i?id=1", "status": {"code": "0"}})

    await client.create_invoice(
        account="1",
        amount_tiyin=100000,
        lines=[{"name": "x", "amount_tiyin": 100000, "quantity": 1}],
        transport=httpx.MockTransport(handler),
    )
    item = seen[0]["items"][0]
    assert isinstance(item["details"], dict), "details must be an object, not a list"
    assert set(item["details"]) == {"name", "values"}
    assert "code" not in item, "no fiscal code configured — omit it, do not send empty"


class TestInvoiceItems:
    def test_lines_carry_the_fiscal_code_and_integer_amounts(self):
        items = client._invoice_items(
            [{"name": "Turkey 3GB / 30 kun", "amount_tiyin": 259900, "quantity": 2}]
        )
        assert items == [
            {
                "items_id": "1",
                "name": "Turkey 3GB / 30 kun",
                "amount": 259900,
                "quantity": 2,
                "details": {"name": "ikpu", "values": settings.atmos_ikpu_code},
                "code": settings.atmos_ikpu_code,
            }
        ]
        # The payload must be JSON-serialisable as-is.
        json.dumps(items)


class TestARefusalIsAnnounced:
    """Every refusal reaches a human within seconds.

    Written the day a customer paid 79 999 so‘m and got nothing: the callback
    was refused because the caller's address had become Cloudflare's, and the
    entire trace was one warning line. We learned about it from the customer.
    A refusal is rare and always means somebody may have been charged, so the
    silent path is the bug.
    """

    async def test_a_bad_ip_raises_the_alarm(self, monkeypatch) -> None:
        sent: list[str] = []

        async def fake_send(text: str, **_kwargs) -> None:
            sent.append(text)

        monkeypatch.setattr("app.integrations.telegram.send_html", fake_send)
        await service.alarm_bad_ip("162.158.172.93")
        assert len(sent) == 1
        assert "162.158.172.93" in sent[0]
        assert "rad etildi" in sent[0]

    async def test_a_telegram_outage_never_breaks_the_answer(self, monkeypatch) -> None:
        """ATMOS is waiting on the response. A failed alarm must not turn a
        refusal into a timeout they will retry."""

        async def broken(_text: str, **_kwargs) -> None:
            raise RuntimeError("telegram is down")

        monkeypatch.setattr("app.integrations.telegram.send_html", broken)
        await service.alarm_bad_ip("1.2.3.4")  # must not raise
