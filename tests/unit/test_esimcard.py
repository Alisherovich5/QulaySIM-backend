"""The eSIMCard client, against its real contract rather than a friendly one.

Every fixture here mirrors a response observed on the live API, because the two
things most likely to cost money are both invisible in the published doc: a
refusal arrives as HTTP 200 with `status: false`, and the retired host answers
every path with 410.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.integrations import esimcard
from app.integrations.esimcard import (
    EsimCardClient,
    EsimCardError,
    EsimCardPurchaseUncertainError,
)
from app.integrations.esimcard_sync import activation_payload

PACKAGE = "bf4e9e42-934f-46e7-9d18-148e4d38a93d"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "esimcard_api_token", "1241694|token")
    monkeypatch.setattr(
        settings, "esimcard_base_url", "https://portal.esimcard.com/api/developer/reseller"
    )


def _client(handler) -> EsimCardClient:
    return EsimCardClient(transport=httpx.MockTransport(handler))


class TestRefusalIsNotSuccess:
    def test_insufficient_balance_arrives_as_http_200_and_still_raises(self):
        # Verbatim from the live API with a zero wallet. A client trusting the
        # status line would report this purchase as complete.
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": False,
                    "message": (
                        "Insufficient Wallet Balance, please refill your wallet and try again"
                    ),
                },
            )

        with pytest.raises(EsimCardError, match="Insufficient Wallet Balance"):
            _client(handler).purchase(package_type_id=PACKAGE)

    def test_the_retired_host_is_named_as_a_stale_url_not_a_failed_purchase(self, monkeypatch):
        monkeypatch.setattr(
            settings, "esimcard_base_url", "https://esimcard.com/api/developer/reseller"
        )

        def handler(request):
            return httpx.Response(
                410, json={"status": False, "message": "API moved to https://portal.esimcard.com"}
            )

        with pytest.raises(EsimCardError, match="stale"):
            _client(handler).balance_usd()

    def test_a_bad_token_says_so(self):
        def handler(request):
            return httpx.Response(401, json={"message": "Unauthenticated."})

        with pytest.raises(EsimCardError, match="token"):
            _client(handler).balance_usd()

    def test_an_unconfigured_client_never_reaches_the_network(self, monkeypatch):
        monkeypatch.setattr(settings, "esimcard_api_token", "")

        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("no request may be made without a token")

        client = _client(handler)
        assert client.is_configured is False
        with pytest.raises(EsimCardError, match="not configured"):
            client.purchase(package_type_id=PACKAGE)


class TestPurchase:
    def test_an_instant_purchase_yields_the_esim_id_and_iccid(self):
        def handler(request):
            assert request.url.path.endswith("/package/purchase")
            import json

            assert json.loads(request.content) == {"package_type_id": PACKAGE}
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {
                        "sim_applied": True,
                        "sim": {"id": "74806e1d", "iccid": "8910000001", "status": "Installed"},
                    },
                },
            )

        bought = _client(handler).purchase(package_type_id=PACKAGE)
        assert bought.applied is True
        assert (bought.supplier_id, bought.iccid, bought.status) == (
            "74806e1d",
            "8910000001",
            "Installed",
        )

    def test_a_delayed_purchase_is_a_success_with_nothing_to_deliver_yet(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {"sim_applied": False, "message": "please wait 2 minutes"},
                },
            )

        bought = _client(handler).purchase(package_type_id=PACKAGE)
        # The money is spent, so this must not read as a failure.
        assert bought.applied is False
        assert bought.supplier_id == ""
        assert "2 minutes" in bought.message

    def test_a_timeout_is_uncertain_not_failed(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        # The distinction is the whole point: a failure may be retried
        # elsewhere, an unknown outcome may not be retried at all.
        with pytest.raises(EsimCardPurchaseUncertainError):
            _client(handler).purchase(package_type_id=PACKAGE)
        assert issubclass(EsimCardPurchaseUncertainError, EsimCardError)


class TestListingAndLookup:
    def _pages(self, journal):
        def handler(request):
            journal.append(str(request.url))
            page = int(httpx.URL(str(request.url)).params.get("page", "1"))
            rows = [
                {
                    "id": f"sim-{page}",
                    "iccid": f"891000000{page}",
                    "status": "Released",
                    "last_bundle": "10gb",
                    "universal_link": f"https://esimsetup.apple.com/esim_qrcode_provisioning?carddata=LPA{page}",
                }
            ]
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "meta": {"total": 3, "perPage": 15, "currentPage": page, "lastPage": 3},
                    "data": rows,
                },
            )

        return handler

    def test_a_page_is_parsed_with_its_last_page_number(self):
        rows, last = _client(self._pages([])).list_esims(page=1)
        assert last == 3
        assert rows[0].supplier_id == "sim-1"
        assert rows[0].universal_link.endswith("carddata=LPA1")

    def test_lookup_stops_as_soon_as_everything_wanted_is_found(self):
        journal: list[str] = []
        found = _client(self._pages(journal)).find_esims({"sim-1"})
        assert set(found) == {"sim-1"}
        assert len(journal) == 1, "must not keep paging once the target is found"

    def test_lookup_walks_pages_until_it_finds_a_later_esim(self):
        journal: list[str] = []
        found = _client(self._pages(journal)).find_esims({"sim-3"})
        assert set(found) == {"sim-3"}
        assert len(journal) == 3

    def test_lookup_is_bounded_so_one_stuck_order_cannot_walk_the_whole_history(self):
        journal: list[str] = []
        found = _client(self._pages(journal)).find_esims({"never-exists"}, max_pages=2)
        assert found == {}
        assert len(journal) == 2


class TestActivationPayload:
    def test_the_lpa_string_is_taken_out_of_the_apple_link(self):
        link = (
            "https://esimsetup.apple.com/esim_qrcode_provisioning"
            "?carddata=LPA:1$rsp.example.com$ABC-123"
        )
        assert activation_payload(link) == "LPA:1$rsp.example.com$ABC-123"

    def test_an_unparseable_link_is_kept_whole_rather_than_dropped(self):
        # A link we cannot parse is still tappable; an empty QR is not.
        assert activation_payload("https://example.com/x") == "https://example.com/x"

    def test_nothing_in_nothing_out(self):
        assert activation_payload("") == ""


def test_balance_is_read_as_a_number(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"status": True, "balance": 0})

    assert _client(handler).balance_usd() == 0.0
    assert esimcard.EsimCardClient(transport=httpx.MockTransport(handler)).is_configured
