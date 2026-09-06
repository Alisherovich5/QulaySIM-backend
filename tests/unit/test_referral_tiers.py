"""Referal komissiyasining pog'onalari.

Kelishuv hali qat'iy emas: bir suhbatda "har bir mijozdan 5%, 100 tadan keyin
6%" ham, "5000, keyin 6000" ham aytilgan. Shuning uchun mexanizm ikkala
ko'rinishni ham ko'taradi va raqam o'zgarganda kod emas, sozlama o'zgaradi.
Shu yerdagi testlar aynan shu ikkiga ayrilishni ushlab turadi.
"""

from decimal import Decimal

import pytest

from app.domain.referral import next_tier, parse_tiers, tier_for


class TestParsing:
    def test_reads_percent_and_flat_in_one_list(self) -> None:
        tiers = parse_tiers("0:5%,100:6000")

        assert tiers[0].percent == Decimal("5")
        assert tiers[0].flat_uzs is None
        assert tiers[1].flat_uzs == 6000
        assert tiers[1].percent is None

    def test_sorts_by_threshold_whatever_the_order_written(self) -> None:
        tiers = parse_tiers("300:6.5%,0:5%,100:6%")

        assert [t.from_count for t in tiers] == [0, 100, 300]

    def test_a_typo_is_refused_rather_than_dropped(self) -> None:
        """Jimgina tashlab yuborish komissiyani jimgina o'zgartirar edi."""

        with pytest.raises(ValueError):
            parse_tiers("0:5%,yuz:6%")
        with pytest.raises(ValueError):
            parse_tiers("besh foiz")

    def test_empty_promises_nothing(self) -> None:
        tiers = parse_tiers("")

        assert tier_for(tiers, 0).amount_for(Decimal("150000")) == 0


class TestRate:
    def test_the_written_example(self) -> None:
        """#2002: "150 minglik olsa 7500 bo'ladi"."""

        tiers = parse_tiers("0:5%,100:6%,300:6.5%")

        assert tier_for(tiers, 0).amount_for(Decimal("150000")) == 7500
        assert tier_for(tiers, 100).amount_for(Decimal("150000")) == 9000
        assert tier_for(tiers, 300).amount_for(Decimal("150000")) == 9750

    def test_more_than_a_hundred_means_the_hundred_and_first(self) -> None:
        """ "100 tadan ko'p" -- 100-mijoz hali eski stavkada."""

        tiers = parse_tiers("0:5%,100:6%")

        assert tier_for(tiers, 99).label == "5%"
        assert tier_for(tiers, 100).label == "6%"

    def test_rounds_to_the_nearest_som(self) -> None:
        """Tiyin yo'q, va pastga yaxlitlash har safar agent zarariga bo'lardi."""

        tiers = parse_tiers("0:5%")

        assert tiers[0].amount_for(Decimal("149999")) == 7500

    def test_a_flat_rate_ignores_the_order_size(self) -> None:
        tiers = parse_tiers("0:5000")

        assert tiers[0].amount_for(Decimal("150000")) == 5000
        assert tiers[0].amount_for(Decimal("1500000")) == 5000

    def test_a_list_that_does_not_start_at_zero_uses_its_lowest_rate(self) -> None:
        """Sozlamada birinchi pog'ona 1 dan boshlansa ham, hech kim stavkasiz
        qolmasligi kerak -- eng pastkisi kafolat sifatida ishlaydi."""

        tiers = parse_tiers("1:5%,100:6%")

        assert tier_for(tiers, 0).label == "5%"


class TestNextRate:
    def test_says_what_is_coming(self) -> None:
        tiers = parse_tiers("0:5%,100:6%,300:6.5%")

        assert next_tier(tiers, 0).from_count == 100
        assert next_tier(tiers, 100).from_count == 300

    def test_nothing_after_the_top(self) -> None:
        tiers = parse_tiers("0:5%,100:6%")

        assert next_tier(tiers, 100) is None


class TestLabel:
    def test_drops_a_trailing_zero_so_six_point_five_stays_readable(self) -> None:
        assert parse_tiers("0:6.50%")[0].label == "6.5%"
        assert parse_tiers("0:6%")[0].label == "6%"
        assert parse_tiers("0:6000")[0].label == "6\u00a0000 so'm"
