"""USD→UZS display rate, cached in Redis so every worker shares one value."""

from __future__ import annotations

from decimal import Decimal

from app.core.cache import cache_key, get_or_set
from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.cbu import RateUnavailableError, fetch_usd_rate
from app.schemas.base import JSONDict

logger = get_logger(__name__)


async def usd_to_uzs() -> JSONDict:
    async def produce() -> JSONDict:
        try:
            rate, updated_at = await fetch_usd_rate(settings.cbu_currency_url)
            return {"usd_to_uzs": rate, "updated_at": updated_at, "source": "cbu"}
        except RateUnavailableError as exc:
            # A CBU outage must never stop customers browsing plans.
            logger.warning("currency.fallback_used", error=str(exc))
            return {
                "usd_to_uzs": settings.uzs_per_usd_fallback,
                "updated_at": None,
                "source": "fallback",
            }

    return await get_or_set(cache_key("currency"), settings.cache_ttl_currency, produce)


# Below this the rounding is worth less than the noise it would add: nothing in
# the catalogue is this cheap, and the degenerate cases (a step larger than the
# amount itself) all live here.
_CHARM_FLOOR = 5_000


def _reads_low(n: int) -> bool:
    """Does this figure already have a 9 in its second digit?

    990 000 and 19 000 are what this function *produces* at one scale, and a
    multiple of ten steps at the next scale down — so a second pass would cut
    them again, to 989 000 and 18 900, and a price would walk downward every
    time it was formatted. The second digit is what the eye reads, so a 9 there
    means the work is already done.
    """
    digits = str(n)
    return len(digits) >= 2 and digits[1] == "9"


def charm_uzs(amount: Decimal | int | float) -> Decimal:
    """The som figure a customer sees, one notch below the round number above it.

    A converted price lands on whatever the day's rate makes it — 220 439 so'm —
    which reads as a number nobody chose. Shops here quote 199 000 and 99 900
    instead, because a leading digit that drops is read as a lower price even
    when the difference is a rounding error. Same reason 100 000 becomes 99 000.

    The rule: floor to `step`, then drop one more step when that lands exactly on
    a round multiple of ten steps — so 100 000 → 99 000 and 30 000 → 29 000, but
    29 400 is already fine and stays put. Step grows with the number, because
    1 000 so'm off a 12 000 so'm plan is a real discount while 1 000 off a
    million is invisible.

    Always downward. Rounding up would show a price we then had to justify, and
    the most it ever gives away is one step — against margins that start at 15%
    and a floor in dollars, that is affordable. It is never applied twice: the
    output of this function is already a fixed point of it.
    """
    a = int(amount)
    if a < _CHARM_FLOOR:
        return Decimal(a)

    if a < 20_000:
        step = 100
    elif a < 1_000_000:
        step = 1_000
    else:
        step = 10_000

    floored = (a // step) * step
    if floored % (step * 10) == 0 and not _reads_low(floored):
        floored -= step
    return Decimal(floored)
