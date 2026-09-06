"""Referral code generation and reward rules — pure logic."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 8
REWARD_PERCENT = 10
REWARD_PREFIX = "REF-"

# Repeat-purchase cashback. A separate prefix so a glance at a code says which
# scheme paid for it, and so support can tell a customer why they have it.
LOYALTY_PREFIX = "QAYT-"


def new_referral_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_reward_code() -> str:
    return REWARD_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(6))


def new_loyalty_code() -> str:
    return LOYALTY_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(6))


def normalise_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = code.strip().upper()
    return cleaned or None


# --- Komissiya pog'onalari ---------------------------------------------------
#
# Shakl bitta, raqamlar esa hali kelishilmagan: bir suhbatda ham "har bir
# mijozdan 5%, 100 tadan keyin 6%", ham "5000, keyin 6000" deyildi. Ikkalasi
# ham bir xil mexanizm -- olib kelingan mijozlar soniga qarab o'sadigan stavka.
# Shuning uchun stavka turi (foiz yoki qat'iy summa) ham sozlamada, ya'ni
# kelishuv o'zgarganda kod emas, bitta qator o'zgaradi.


@dataclass(frozen=True)
class CommissionTier:
    """Shu sondan boshlab amal qiladigan stavka."""

    from_count: int
    percent: Decimal | None = None
    flat_uzs: int | None = None

    def amount_for(self, order_uzs: Decimal) -> int:
        if self.flat_uzs is not None:
            return int(self.flat_uzs)
        if self.percent is None:
            return 0
        value = (Decimal(order_uzs) * self.percent / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(value)

    @property
    def label(self) -> str:
        """Ekranga chiqadigan ko'rinish. So'm summalari uzun bo'ladi, ajratmasdan
        o'qib bo'lmaydi -- shuning uchun mingliklar bo'sh joy bilan ajratiladi."""

        if self.flat_uzs is not None:
            return f"{self.flat_uzs:,}".replace(",", "\u00a0") + " so'm"
        return f"{_trim(self.percent)}%"


def _trim(value: Decimal | None) -> str:
    """`6.50` -> `6.5`, `6.00` -> `6`. Yorliq ekranda ko'rinadi, ortiqcha nol
    stavkani aniqroq qilmaydi, faqat uzunroq qiladi."""

    if value is None:
        return "0"
    return format(value.normalize(), "f")


def parse_tiers(raw: str) -> list[CommissionTier]:
    """`"0:5%,100:6%,300:6.5%"` yoki `"0:5000,100:6000"` ni o'qiydi.

    Xato yozilgan bo'lak jimgina tashlanmaydi -- u komissiyani jimgina
    o'zgartirar edi. Bo'sh satr esa bitta 0% pog'ona: hech kimga hech narsa
    va'da qilinmaydi.
    """

    tiers: list[CommissionTier] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"pog'ona noto'g'ri: {chunk!r}")
        head, value = (part.strip() for part in chunk.split(":", 1))
        if not head.isdigit():
            raise ValueError(f"pog'ona boshi son bo'lishi kerak: {chunk!r}")
        if value.endswith("%"):
            tiers.append(CommissionTier(int(head), percent=Decimal(value[:-1])))
        else:
            tiers.append(CommissionTier(int(head), flat_uzs=int(value)))
    if not tiers:
        return [CommissionTier(0, percent=Decimal(0))]
    return sorted(tiers, key=lambda t: t.from_count)


def tier_for(tiers: list[CommissionTier], index: int) -> CommissionTier:
    """`index` -- bu nechanchi mijoz (0 dan boshlanadi).

    Stavka mijoz olib kelingan paytdagi pog'ona bo'yicha olinadi, keyin
    o'zgarmaydi. Aks holda 100-mijoz kelganda oldingi 99 tasining puli ham
    qayta hisoblanib, balans o'zgarib ketardi -- o'zgarib turadigan balansga
    hech kim ishonmaydi.
    """

    chosen = tiers[0]
    for tier in tiers:
        if index + 1 > tier.from_count:
            chosen = tier
        else:
            break
    return chosen


def next_tier(tiers: list[CommissionTier], count: int) -> CommissionTier | None:
    """Keyingi pog'ona, agar bo'lsa -- panelda "yana nechta kerak" deyish uchun."""

    for tier in tiers:
        if count < tier.from_count:
            return tier
    return None
