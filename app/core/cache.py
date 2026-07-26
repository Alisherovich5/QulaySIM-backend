"""Redis-backed cache.

Replaces the previous module-level dictionaries, which were per-process and
therefore useless behind more than one worker (and leaked memory).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
from redis.asyncio import Redis, from_url

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis: Redis[bytes] | None = None


def get_redis() -> Redis[bytes]:
    global _redis
    if _redis is None:
        _redis = from_url(
            str(settings.redis_url),
            encoding="utf-8",
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()  # type: ignore[attr-defined]
        _redis = None


def cache_key(namespace: str, **parts: Any) -> str:
    if not parts:
        return f"qs:{namespace}"
    raw = orjson.dumps(parts, option=orjson.OPT_SORT_KEYS)
    return f"qs:{namespace}:{hashlib.sha256(raw).hexdigest()[:16]}"


async def get_or_set[T](
    key: str,
    ttl: int,
    producer: Callable[[], Awaitable[T]],
) -> T:
    """Read-through cache. A Redis outage degrades to a direct call rather
    than taking the endpoint down with it."""
    redis = get_redis()
    try:
        cached = await redis.get(key)
        if cached is not None:
            return orjson.loads(cached)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.read_failed", key=key, error=str(exc))

    value = await producer()

    try:
        await redis.set(key, orjson.dumps(value, default=str), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.write_failed", key=key, error=str(exc))
    return value


async def invalidate(*patterns: str) -> int:
    """Delete keys by glob. Uses SCAN, never KEYS, so it stays safe on a
    production-sized keyspace."""
    redis = get_redis()
    removed = 0
    try:
        for pattern in patterns:
            async for key in redis.scan_iter(match=pattern, count=500):
                removed += await redis.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.invalidate_failed", error=str(exc))
    return removed
