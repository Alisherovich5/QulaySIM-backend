"""What the router believes when a wholesaler will not say what it holds.

Measured rather than imagined. At 08:45 on 22 September the worker restarted,
its thirty-second balance cache was cold, and the live read of eSIMCard's
balance timed out. Unknown means attempt, so the order went to a wallet holding
24 cents and was refused six times — for a number the wallet watch had recorded
correctly ten minutes earlier and was still holding.

The fallback is narrow: only when the live read produced nothing, only from the
watch's own key, and only for the provider that failed. A recorded number is at
most WATCH_TTL old, and being wrong with it costs one five-minute rescue cycle
because the next sweep has a fresh reading. Being wrong without it costs a
round trip, a failed row in the ledger and an alarm, every single time.
"""

from __future__ import annotations

import json

import pytest

from app.integrations import wallets


class FakeRedis:
    def __init__(self, store: dict[str, bytes] | None = None, broken: bool = False) -> None:
        self.store = store or {}
        self.broken = broken

    def get(self, key: str) -> bytes | None:
        if self.broken:
            raise OSError("redis is gone")
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        if self.broken:
            raise OSError("redis is gone")
        self.store[key] = value.encode()


@pytest.fixture
def redis_with(monkeypatch):
    def use(store=None, broken=False):
        fake = FakeRedis(store, broken)
        monkeypatch.setattr(wallets, "_redis", lambda: fake)
        return fake

    return use


def watch(**values: float) -> dict[str, bytes]:
    return {wallets.WATCH_KEY: json.dumps(values).encode()}


class TestASupplierThatDidNotAnswer:
    def test_falls_back_to_what_the_watch_recorded(self, redis_with, monkeypatch) -> None:
        redis_with(watch(esimcard=0.24, esimaccess=45.48))
        monkeypatch.setattr(
            wallets, "fetch_balances", lambda: {"esimcard": None, "esimaccess": 45.48}
        )

        assert wallets.balances() == {"esimcard": 0.24, "esimaccess": 45.48}

    def test_stays_unknown_when_the_watch_has_nothing_either(
        self, redis_with, monkeypatch
    ) -> None:
        # Nothing recorded, so nothing to fall back to — and unknown still
        # means attempt, because a paid order must not be stranded by a
        # monitoring gap.
        redis_with({})
        monkeypatch.setattr(wallets, "fetch_balances", lambda: {"esimcard": None})

        assert wallets.balances() == {"esimcard": None}

    def test_only_the_provider_that_failed_is_substituted(
        self, redis_with, monkeypatch
    ) -> None:
        # The live number wins wherever there is one: the watch is a fallback,
        # never an override.
        redis_with(watch(esimcard=0.24, esimaccess=10.00))
        monkeypatch.setattr(
            wallets, "fetch_balances", lambda: {"esimcard": None, "esimaccess": 45.48}
        )

        assert wallets.balances() == {"esimcard": 0.24, "esimaccess": 45.48}

    def test_a_broken_redis_leaves_it_unknown(self, redis_with, monkeypatch) -> None:
        redis_with(broken=True)
        monkeypatch.setattr(wallets, "fetch_balances", lambda: {"esimcard": None})

        assert wallets.balances() == {"esimcard": None}


class TestTheLiveCacheStillWins:
    def test_a_warm_cache_is_not_refetched(self, redis_with, monkeypatch) -> None:
        redis_with({wallets.LIVE_KEY: json.dumps({"esimcard": 7.0}).encode()})
        monkeypatch.setattr(
            wallets, "fetch_balances", lambda: pytest.fail("should not have asked")
        )

        assert wallets.balances() == {"esimcard": 7.0}

    def test_a_reading_of_zero_is_a_reading(self, redis_with, monkeypatch) -> None:
        # bool(0.0) is False: a wallet that really holds nothing must not be
        # mistaken for one that did not answer and quietly replaced.
        redis_with(watch(esimcard=99.0))
        monkeypatch.setattr(wallets, "fetch_balances", lambda: {"esimcard": 0.0})

        assert wallets.balances() == {"esimcard": 0.0}
