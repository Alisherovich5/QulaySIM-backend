"""Extra data for an eSIM somebody already owns.

A customer who bought Turkey 1 GB and used it up wants another gigabyte on the
same profile — not a second QR code to install, and not a second eSIM slot on a
phone that has four. The wholesaler supports exactly that, and this is the path
to it.

Two facts shape everything here.

The wholesaler quotes top-ups **per eSIM**: `package/list` refuses to answer for
a country, only for an ICCID (or a package code, or a transaction). So there is
no nightly sync that could pre-price these the way the rest of the catalogue is
priced — the cost only exists at the moment somebody asks. That is why pricing
happens in this module, reading the same markup rules the admin manages, rather
than hardcoding a second ladder that would drift from the first.

And a top-up creates no new eSIM row. Everything downstream that asks "was this
order delivered?" by looking for an eSIM would therefore call every top-up
undelivered forever, which is why the line carries `topup_applied_at` and why
the sweep reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ESIM, PricingRule
from app.services.currency import charm_uzs, usd_to_uzs

logger = get_logger("topups")

#: Only this wholesaler sells top-ups today. eSIMCard's catalogue has no
#: equivalent endpoint, and an eSIM bought from them simply offers none — which
#: the account page says plainly rather than showing an empty list.
TOPUP_PROVIDERS = ("esimaccess",)

#: The supplier reports money in 1/10000 USD, the same unit as everywhere else in
#: their API.
PRICE_DIVISOR = Decimal("10000")

#: Cap on what a single top-up may cost the customer, as a guard against a
#: mispriced or misparsed package reaching the payment page.
MAX_PRICE_USD = Decimal("500")

#: Wholesaler states in which more data can still be attached to the profile.
#:
#: `USED_UP` belongs here and that is the whole point. It means the ALLOWANCE is
#: gone, not the validity -- which is exactly the customer a top-up exists for.
#: Our own `status` column collapses it to "expired" together with the states
#: where time really has run out, so reading that column refused the sale to the
#: one person who wanted to make it. Two customers were blocked this way, with
#: 20 and 11 days left on their plans.
TOPPABLE_PROVIDER_STATUSES = frozenset({"IN_USE", "GOT_RESOURCE", "USED_UP"})


def is_toppable(esim: ESIM, *, now: datetime | None = None) -> bool:
    """Whether the wholesaler will still add data to this profile.

    Two independent ways to be finished, and only one of them blocks a top-up:
    the allowance can be spent (topping up is the remedy) or the validity can
    have elapsed (nothing to attach data to).
    """

    if esim.provider not in TOPUP_PROVIDERS or not esim.iccid:
        return False
    if (esim.provider_status or "") not in TOPPABLE_PROVIDER_STATUSES:
        return False
    if esim.expires_at is not None:
        moment = now or datetime.now(UTC)
        expires = esim.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= moment:
            return False
    return True


@dataclass(frozen=True)
class TopUp:
    """One purchasable top-up for one eSIM."""

    package_code: str
    name: str
    data_mb: int
    validity_days: int
    cost_usd: Decimal
    price_usd: Decimal

    @property
    def data_label(self) -> str:
        if self.data_mb % 1024 == 0:
            return f"{self.data_mb // 1024} GB"
        return f"{self.data_mb} MB"


async def _rule_for(session: AsyncSession, data_mb: int, days: int) -> PricingRule | None:
    """The pricing rule a top-up of this shape falls under, or None.

    Resolution is the narrow version of what Django does: a tier rule for this
    exact size (optionally pinned to a duration) wins, otherwise the house
    default. The per-destination and per-supplier scopes are deliberately not
    consulted -- they exist to correct a specific destination's retail price, and
    a top-up is priced off a cost the wholesaler quoted seconds ago rather than
    off that destination's ladder.
    """

    rules = (
        (await session.execute(select(PricingRule).where(PricingRule.is_active.is_(True))))
        .scalars()
        .all()
    )
    tier = [
        rule
        for rule in rules
        if rule.scope == "tier" and rule.tier_data_mb == data_mb and rule.tier_days in (None, days)
    ]
    if tier:
        # Most specific first: a rule naming the duration beats one that does not.
        tier.sort(key=lambda rule: (rule.tier_days is None,))
        return tier[0]
    house = [rule for rule in rules if rule.scope == "global"]
    return house[0] if house else None


def _markup_of(rule: PricingRule | None, data_mb: int, days: int) -> Decimal:
    """The markup percentage this rule carries, with a last-resort default.

    No rules configured at all is a configuration question, not a reason to take
    top-ups offline; 50% is the house default in the admin's own seed.
    """

    if rule is not None:
        return Decimal(rule.markup_percent)
    logger.warning("topups.no_pricing_rule", data_mb=data_mb, days=days)
    return Decimal("50")


def _floor_of(rule: PricingRule | None) -> Decimal | None:
    """The rule's absolute margin floor in dollars, if it sets one."""

    if rule is None or rule.min_margin_usd is None:
        return None
    return Decimal(rule.min_margin_usd)


def _price(cost: Decimal, markup: Decimal, floor_usd: Decimal | None = None) -> Decimal:
    """Cost plus the markup, but never less than the rule's absolute floor.

    The floor was being ignored here while Django applied it, so the two ladders
    the module docstring promises are the same one had in fact drifted. On the
    thin tiers -- 10 GB is +25%, 50 GB is +15% -- that is the difference between
    a top-up carrying a margin and a top-up quoting a number a few cents above
    what we paid for it, which is what a customer photographed and sent in.
    """

    price = cost * (Decimal("1") + markup / Decimal("100"))
    if floor_usd is not None and floor_usd > 0:
        price = max(price, cost + floor_usd)
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _volume_mb(package: dict[str, Any]) -> int:
    """Bytes to megabytes, the way the rest of this integration reads volume."""
    raw = package.get("volume") or 0
    try:
        return int(int(raw) / (1024 * 1024))
    except (TypeError, ValueError):
        return 0


async def available(session: AsyncSession, esim: ESIM) -> list[TopUp]:
    """What this eSIM can be topped up with, priced for the customer.

    Returns an empty list rather than raising when the supplier cannot be
    reached: the account page then says top-ups are unavailable right now, which
    is true and recoverable, instead of failing the whole page.
    """
    # Ro'yxat ham, to'lov ham bitta qoidaga tayanadi. Aks holda mijoz narxlarni
    # ko'rib, tanlab, to'lov paytida "bo'lmaydi" degan javob oladi -- aynan shu
    # bo'lgan edi.
    if not is_toppable(esim):
        return []

    from app.integrations.esim_access import EsimAccessClient, EsimAccessError

    client = EsimAccessClient()
    if not client.is_configured:
        return []

    try:
        # Blocking HTTP inside async: the supplier client is sync and this is one
        # call on a page a customer explicitly asked for. Worth revisiting if it
        # ever lands on a hot path.
        response = client.list_packages(package_type="TOPUP", iccid=esim.iccid)
    except EsimAccessError as exc:
        logger.warning("topups.list_failed", esim_id=esim.id, error=str(exc))
        return []

    packages = ((response.get("obj") or {}).get("packageList")) or []
    out: list[TopUp] = []
    for package in packages:
        code = str(package.get("packageCode") or "")
        data_mb = _volume_mb(package)
        days = int(package.get("duration") or 0)
        raw_price = package.get("price")
        if not code or data_mb <= 0 or days <= 0 or raw_price in (None, ""):
            continue
        cost = (Decimal(str(raw_price)) / PRICE_DIVISOR).quantize(Decimal("0.01"))
        if cost <= 0:
            continue
        rule = await _rule_for(session, data_mb, days)
        price = _price(cost, _markup_of(rule, data_mb, days), _floor_of(rule))
        # A top-up that would sell at or below cost is not offered at all. Same
        # rule as the catalogue: an order that loses money is worse than an
        # option the customer never saw.
        if price <= cost or price > MAX_PRICE_USD:
            logger.warning(
                "topups.refused", code=code, cost=str(cost), price=str(price), esim_id=esim.id
            )
            continue
        out.append(
            TopUp(
                package_code=code,
                name=str(package.get("name") or ""),
                data_mb=data_mb,
                validity_days=days,
                cost_usd=cost,
                price_usd=price,
            )
        )
    # Smallest first: somebody who ran out mid-trip usually wants the cheapest
    # thing that gets them through the day.
    out.sort(key=lambda item: (item.data_mb, item.validity_days))
    return out


async def find(session: AsyncSession, esim: ESIM, package_code: str) -> TopUp | None:
    """The one the customer chose, re-read from the supplier at purchase time.

    Never trusts a price that came back from the browser: the list was fetched
    seconds or minutes ago, and the amount charged has to be the amount the
    wholesaler will honour now.
    """
    for option in await available(session, esim):
        if option.package_code == package_code:
            return option
    return None


async def as_som(price_usd: Decimal) -> Decimal:
    """The som figure shown next to a top-up, on the same rule as every price.

    Display only: what the card is actually charged is frozen when the order is
    placed, by the same code that freezes it for an ordinary purchase.
    """
    rate = (await usd_to_uzs()).get("usd_to_uzs")
    if not rate:
        return Decimal("0")
    return charm_uzs(price_usd * Decimal(str(rate)))
