"""A fallback rate must not keep the shop shut for six hours.

Checkout refuses to price an order against anything but a CBU rate, so the
cached rate is not merely a display value — it is the thing that decides whether
anyone can pay. The real rate is good for six hours; a fallback cached for the
same six hours is an outage that outlives its cause by six hours. It happened
once: a restart raced the network, the fallback landed in Redis, and orders were
refused until the key was deleted by hand.
"""

from __future__ import annotations

import pytest

from app.core.cache import cache_key
from app.core.config import settings
from app.integrations.cbu import RateUnavailableError
from app.services import currency


@pytest.fixture(autouse=True)
async def _clear_currency_cache():
    from app.core.cache import get_redis

    await get_redis().delete(cache_key("currency"))
    yield
    await get_redis().delete(cache_key("currency"))


async def test_a_real_rate_keeps_the_full_ttl(monkeypatch) -> None:
    async def ok(_url):
        return 12345.0, "07.08.2026"

    monkeypatch.setattr(currency, "fetch_usd_rate", ok)
    payload = await currency.usd_to_uzs()

    assert payload["source"] == "cbu"
    from app.core.cache import get_redis

    ttl = await get_redis().ttl(cache_key("currency"))
    # Cached for hours, not seconds — the rate moves once a day.
    assert ttl > currency._FALLBACK_TTL
    assert ttl <= settings.cache_ttl_currency


async def test_a_fallback_expires_within_the_minute(monkeypatch) -> None:
    async def down(_url):
        raise RateUnavailableError("cbu unreachable")

    monkeypatch.setattr(currency, "fetch_usd_rate", down)
    payload = await currency.usd_to_uzs()

    assert payload["source"] == "fallback"
    from app.core.cache import get_redis

    ttl = await get_redis().ttl(cache_key("currency"))
    assert 0 < ttl <= currency._FALLBACK_TTL


async def test_recovery_is_not_blocked_by_the_cached_fallback(monkeypatch) -> None:
    """The point of the short TTL: CBU coming back reopens checkout.

    With the six-hour TTL this test would still see the fallback, which is
    precisely the failure — the shop stayed closed after the cause was gone.
    """

    async def down(_url):
        raise RateUnavailableError("cbu unreachable")

    monkeypatch.setattr(currency, "fetch_usd_rate", down)
    assert (await currency.usd_to_uzs())["source"] == "fallback"

    from app.core.cache import get_redis

    # Stand in for the minute passing, rather than sleeping through it.
    await get_redis().delete(cache_key("currency"))

    async def ok(_url):
        return 11915.64, "07.08.2026"

    monkeypatch.setattr(currency, "fetch_usd_rate", ok)
    recovered = await currency.usd_to_uzs()
    assert recovered["source"] == "cbu"
    assert recovered["usd_to_uzs"] == 11915.64
