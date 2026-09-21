"""What checkout is allowed to assume about a wholesaler's wallet.

`is_fulfillable` used to say, in as many words, that a wallet is not checkout's
business: a wallet can be topped up in a minute, and refusing a sale over a
bookkeeping state would take the shop offline. That holds for exactly as long as
a plan has a second supplier to fall through to. Order #141 is what happens when
it does not — one eSIMCard offer, an empty eSIMCard wallet, $5.91 taken, no eSIM,
three days of the rescue re-dispatching every five minutes. No amount of retrying
buys anything from a wallet with nothing in it.

So the rule here is deliberately narrow: a supplier that demonstrably cannot pay
for *this* plan does not count as a way to supply it. A plan another wholesaler
also stocks still sells; the shop goes quiet only on the ones nobody can deliver.

Two properties this has to keep, and both are about which way it breaks:

* **It reads Redis, never a supplier.** A quote is a customer waiting on a page,
  and a wholesaler's balance endpoint is a third party that can hang. The worker
  refreshes the number on a schedule (`maintenance.check_wallets`) and checkout
  reads whatever was left there.
* **It fails open.** A missing key, a dead Redis, an unparsable balance, a
  supplier that did not answer — all of that means *unknown*, and unknown sells.
  Being wrong that way costs one order the rescue then picks up. Being wrong the
  other way is a shop that refuses money because monitoring broke.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal

from app.core.cache import get_redis
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Written by the wallet watch, read by checkout.
WALLET_KEY = "qs:supplier:wallets"

#: Comfortably longer than the watch's ten-minute cadence, so one missed run does
#: not blind checkout, and short enough that a worker down for an hour stops
#: being quoted as fact.
WALLET_TTL = 45 * 60


async def known_balances() -> dict[str, float]:
    """The last balances the watch recorded. Anything unknown is simply absent."""
    try:
        raw = await get_redis().get(WALLET_KEY)
    except Exception as exc:  # noqa: BLE001 - a cache outage must not block a sale
        logger.warning("wallets.cache_read_failed", error=str(exc))
        return {}
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        logger.warning("wallets.cache_unparsable")
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: float(value)
        for key, value in loaded.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def can_cover(
    provider: str,
    cost: Decimal | float | None,
    balances: Mapping[str, float],
) -> bool:
    """Whether this wallet can pay for one unit of this plan.

    An unknown balance answers yes, and so does an unknown cost: the question
    this answers is "do we already know this will fail", not "are we sure it
    will work".
    """
    balance = balances.get(provider)
    if balance is None:
        return True
    try:
        needed = float(cost or 0)
    except (TypeError, ValueError):
        return True
    return needed <= balance
