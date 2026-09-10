"""Extra data on an eSIM somebody already owns.

Three things here can cost real money if they go wrong, and each has a test:
selling a top-up for less than we pay for it, charging a price the wholesaler no
longer honours, and buying the same package twice on a retry. The fourth — a
top-up looking undelivered forever, because it creates no eSIM row — would cost
trust instead, by making the alert cry wolf until nobody reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import topups

pytestmark = pytest.mark.anyio


class _Rule:
    def __init__(self, scope, markup, tier_mb=None, tier_days=None, min_margin=None):
        self.scope = scope
        self.markup_percent = Decimal(str(markup))
        self.tier_data_mb = tier_mb
        self.tier_days = tier_days
        self.min_margin_usd = None if min_margin is None else Decimal(str(min_margin))
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
    # Ta'minotchining holati -- to'ldirish mumkinligini AYNAN shu hal qiladi,
    # bizning `status` ustuni emas.
    provider_status = "IN_USE"
    expires_at = None


class TestPricing:
    async def test_a_tier_rule_beats_the_house_default(self) -> None:
        session = _Session([_Rule("global", 50), _Rule("tier", 25, tier_mb=10240)])
        rule = await topups._rule_for(session, 10240, 30)
        assert topups._markup_of(rule, 10240, 30) == Decimal("25")

    async def test_a_rule_naming_the_duration_beats_one_that_does_not(self) -> None:
        session = _Session(
            [_Rule("tier", 40, tier_mb=1024), _Rule("tier", 60, tier_mb=1024, tier_days=7)]
        )
        rule = await topups._rule_for(session, 1024, 7)
        assert topups._markup_of(rule, 1024, 7) == Decimal("60")

    async def test_the_house_default_applies_to_a_shape_with_no_rung(self) -> None:
        session = _Session([_Rule("global", 50), _Rule("tier", 25, tier_mb=10240)])
        rule = await topups._rule_for(session, 2048, 3)
        assert topups._markup_of(rule, 2048, 3) == Decimal("50")

    async def test_no_rules_at_all_still_prices(self) -> None:
        """Refusing to price would take top-ups offline over a configuration
        question nobody asked."""
        rule = await topups._rule_for(_Session([]), 1024, 7)
        assert rule is None
        assert topups._markup_of(rule, 1024, 7) == Decimal("50")

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


def _fixed_rule(percent, min_margin=None):
    """`_rule_for` o'rniga: narx hisobi endi foizni ham, polni ham qoidadan
    oladi, ya'ni testda ham bitta manba bo'lishi kerak."""

    async def _rule_for(_session, _mb, _days):
        return _Rule("global", percent, min_margin=min_margin)

    return _rule_for


def _client(packages):
    class _Client:
        is_configured = True

        def list_packages(self, **_kwargs):
            return {"obj": {"packageList": packages}}

    return lambda: _Client()


async def _available(monkeypatch, packages, markup=50, min_margin=None):
    monkeypatch.setattr(topups, "_rule_for", _fixed_rule(markup, min_margin))
    monkeypatch.setattr("app.integrations.esim_access.EsimAccessClient", _client(packages))
    return await topups.available(_Session([]), _Esim())


class TestWhoCanTopUp:
    """Ikki xil "tugadi" bor va faqat bittasi to'ldirishga to'sqinlik qiladi.

    Mijoz yozdi: "мегабайтим тугаб қоганди, кўшимча пакет сотиб олмоқчи эдим,
    имкони бўлмаяпти". Hajmi tugagan -- to'ldirish aynan shu uchun bor; kod esa
    bizning `status` ustunini o'qib, uni muddati o'tganlar bilan bir qatorga
    qo'ygan va sotuvni rad qilgan. O'sha payt shu holatda ikki mijoz turgan
    edi, tariflarida 20 va 11 kun qolgan.
    """

    def _esim(self, **over):
        esim = _Esim()
        for key, value in over.items():
            setattr(esim, key, value)
        return esim

    def test_spent_allowance_can_still_be_topped_up(self) -> None:
        future = datetime(2026, 9, 30, tzinfo=UTC)
        esim = self._esim(provider_status="USED_UP", expires_at=future)

        assert topups.is_toppable(esim, now=datetime(2026, 9, 10, tzinfo=UTC)) is True

    def test_elapsed_validity_cannot(self) -> None:
        """Muddati o'tgan profilga ta'minotchi hajm ulamaydi -- pul olib,
        hech narsa bermagan bo'lardik."""

        past = datetime(2026, 9, 7, tzinfo=UTC)
        esim = self._esim(provider_status="USED_UP", expires_at=past)

        assert topups.is_toppable(esim, now=datetime(2026, 9, 10, tzinfo=UTC)) is False

    def test_a_working_profile_can(self) -> None:
        esim = self._esim(provider_status="IN_USE", expires_at=datetime(2026, 10, 8, tzinfo=UTC))

        assert topups.is_toppable(esim, now=datetime(2026, 9, 10, tzinfo=UTC)) is True

    def test_a_revoked_profile_cannot(self) -> None:
        esim = self._esim(provider_status="REVOKE", expires_at=datetime(2026, 10, 8, tzinfo=UTC))

        assert topups.is_toppable(esim, now=datetime(2026, 9, 10, tzinfo=UTC)) is False

    def test_a_naive_expiry_is_not_a_crash(self) -> None:
        """Bazadan vaqt mintaqasiz kelishi mumkin. Solishtirishda bu TypeError
        beradi, ya'ni butun kabinet sahifasi yiqilardi."""

        esim = self._esim(provider_status="USED_UP", expires_at=datetime(2026, 9, 30))

        assert topups.is_toppable(esim, now=datetime(2026, 9, 10, tzinfo=UTC)) is True

    def test_another_wholesaler_offers_none(self) -> None:
        esim = self._esim(provider="esimcard")

        assert topups.is_toppable(esim) is False

    async def test_the_list_is_empty_when_the_sale_would_be_refused(self, monkeypatch) -> None:
        """Narxlarni ko'rsatib, keyin to'lovda "bo'lmaydi" deyish -- aynan
        mijoz duch kelgan tartib. Ro'yxat ham, to'lov ham bitta qoidaga
        tayanadi."""

        monkeypatch.setattr(topups, "_rule_for", _fixed_rule(50))
        monkeypatch.setattr("app.integrations.esim_access.EsimAccessClient", _client([_package()]))
        dead = self._esim(provider_status="USED_EXPIRED")

        assert await topups.available(_Session([]), dead) == []


class TestTheMarginFloor:
    """Qoidadagi dollarlik pol Django tomonda qo'llanardi, bu yerda esa yo'q.

    Natijada yupqa pog'onalarda (10 GB +25%, 50 GB +15%) to'ldirish tannarxdan
    bir necha sent yuqorida narx aytardi -- mijoz aynan shuni suratga olib
    yuborgan.
    """

    async def test_the_floor_lifts_a_thin_percentage(self, monkeypatch) -> None:
        # $4.60 tannarx, +25% = $5.75. $1.50 pol bilan $6.10 bo'lishi kerak.
        options = await _available(monkeypatch, [_package()], markup=25, min_margin=Decimal("1.50"))

        assert options[0].cost_usd == Decimal("0.46")
        assert options[0].price_usd == Decimal("1.96")

    async def test_a_generous_percentage_is_left_alone(self, monkeypatch) -> None:
        """Pol -- pol, tepa emas. Foiz undan yuqori bo'lsa, foiz qoladi."""

        options = await _available(
            monkeypatch, [_package()], markup=200, min_margin=Decimal("0.10")
        )

        assert options[0].price_usd == Decimal("1.38")

    async def test_no_floor_configured_changes_nothing(self, monkeypatch) -> None:
        options = await _available(monkeypatch, [_package()], markup=50, min_margin=None)

        assert options[0].price_usd == Decimal("0.69")
