"""Extra data on an eSIM somebody already owns.

Three things here can cost real money if they go wrong, and each has a test:
selling a top-up for less than we pay for it, charging a price the wholesaler no
longer honours, and buying the same package twice on a retry. The fourth — a
top-up looking undelivered forever, because it creates no eSIM row — would cost
trust instead, by making the alert cry wolf until nobody reads it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import topups

pytestmark = pytest.mark.anyio


class _Rule:
    def __init__(self, scope, markup, tier_mb=None, tier_days=None):
        self.scope = scope
        self.markup_percent = Decimal(str(markup))
        self.tier_data_mb = tier_mb
        self.tier_days = tier_days
        self.is_active = True


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rules):
        self._rules = rules

    async def execute(self, _statement):
        return _Result(self._rules)


def _package(code="TOPUP_X", gb=1, days=7, price_ten_thousandths=4600):
    """A package in the shape the wholesaler actually sends."""
    return {
        "packageCode": code,
        "name": f"Turkey {gb}GB {days}Days",
        "volume": gb * 1024 * 1024 * 1024,
        "duration": days,
        "price": price_ten_thousandths,
    }


class _Esim:
    id = 7
    provider = "esimaccess"
    iccid = "8997250230001244458"
    plan_id = 1


class TestPricing:
    async def test_a_tier_rule_beats_the_house_default(self) -> None:
        session = _Session([_Rule("global", 50), _Rule("tier", 25, tier_mb=10240)])
        assert await topups._markup(session, 10240, 30) == Decimal("25")

    async def test_a_rule_naming_the_duration_beats_one_that_does_not(self) -> None:
        session = _Session(
            [_Rule("tier", 40, tier_mb=1024), _Rule("tier", 60, tier_mb=1024, tier_days=7)]
        )
        assert await topups._markup(session, 1024, 7) == Decimal("60")

    async def test_the_house_default_applies_to_a_shape_with_no_rung(self) -> None:
        session = _Session([_Rule("global", 50), _Rule("tier", 25, tier_mb=10240)])
        assert await topups._markup(session, 2048, 3) == Decimal("50")

    async def test_no_rules_at_all_still_prices(self) -> None:
        """Refusing to price would take top-ups offline over a configuration
        question nobody asked."""
        assert await topups._markup(_Session([]), 1024, 7) == Decimal("50")

    def test_the_markup_is_applied_to_the_cost(self) -> None:
        assert topups._price(Decimal("0.46"), Decimal("60")) == Decimal("0.74")

    def test_supplier_money_is_read_in_ten_thousandths(self) -> None:
        """Their whole API quotes 1/10000 USD. Reading it as cents once cost a
        balance figure of $500,000 instead of $50."""
        assert Decimal("4600") / topups.PRICE_DIVISOR == Decimal("0.46")


class TestWhatIsOffered:
    async def test_options_are_priced_and_sorted_smallest_first(self, monkeypatch) -> None:
        """Somebody who ran out mid-trip wants the cheapest thing that gets them
        through the day, not the biggest bundle."""
        options = await _available(
            monkeypatch,
            [_package(code="B", gb=5, price_ten_thousandths=23000), _package(code="A", gb=1)],
        )
        assert [option.package_code for option in options] == ["A", "B"]
        assert options[0].price_usd == Decimal("0.69")

    async def test_a_package_that_would_sell_at_a_loss_is_never_offered(self, monkeypatch) -> None:
        """Same rule as the catalogue: an option the customer never saw beats an
        order that loses money."""
        options = await _available(monkeypatch, [_package()], markup=0)
        assert options == []

    async def test_an_absurd_price_is_refused(self, monkeypatch) -> None:
        options = await _available(
            monkeypatch, [_package(price_ten_thousandths=99_000_000)], markup=50
        )
        assert options == []

    async def test_a_malformed_package_is_skipped_not_crashed(self, monkeypatch) -> None:
        """One bad row must not take the whole list with it — the customer would
        see "no top-ups" for an eSIM that has five."""
        options = await _available(
            monkeypatch,
            [
                {"packageCode": "", "volume": 0},
                {"packageCode": "OK", "volume": 1024**3, "duration": 7, "price": 4600},
            ],
            markup=60,
        )
        assert [option.package_code for option in options] == ["OK"]

    async def test_an_esimcard_profile_offers_nothing(self, monkeypatch) -> None:
        """Their API has no equivalent endpoint, so the honest answer is none."""

        class _Other(_Esim):
            provider = "esimcard"

        assert await topups.available(_Session([]), _Other()) == []

    async def test_a_supplier_outage_answers_empty_rather_than_failing(self, monkeypatch) -> None:
        from app.integrations.esim_access import EsimAccessError

        class _Broken:
            is_configured = True

            def list_packages(self, **_kwargs):
                raise EsimAccessError("upstream is down")

        monkeypatch.setattr("app.integrations.esim_access.EsimAccessClient", lambda: _Broken())
        assert await topups.available(_Session([]), _Esim()) == []


class TestLabels:
    def test_whole_gigabytes_read_as_gigabytes(self) -> None:
        option = topups.TopUp("X", "n", 5120, 30, Decimal("1"), Decimal("2"))
        assert option.data_label == "5 GB"

    def test_a_part_gigabyte_keeps_its_megabytes(self) -> None:
        """The wholesaler stocks a 500 MB oddity; calling it "0 GB" would be worse
        than saying nothing."""
        option = topups.TopUp("X", "n", 500, 7, Decimal("1"), Decimal("2"))
        assert option.data_label == "500 MB"


# --- helpers ---------------------------------------------------------------


def _fixed_markup(percent):
    async def _markup(_session, _mb, _days):
        return Decimal(str(percent))

    return _markup


def _client(packages):
    class _Client:
        is_configured = True

        def list_packages(self, **_kwargs):
            return {"obj": {"packageList": packages}}

    return lambda: _Client()


async def _available(monkeypatch, packages, markup=50):
    monkeypatch.setattr(topups, "_markup", _fixed_markup(markup))
    monkeypatch.setattr("app.integrations.esim_access.EsimAccessClient", _client(packages))
    return await topups.available(_Session([]), _Esim())
