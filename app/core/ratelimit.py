"""Distributed fixed-window rate limiting backed by Redis.

The previous implementation used an in-process dict: it did not survive a
restart, did not apply across workers, and grew without bound.
"""

from __future__ import annotations

from fastapi import Request

from app.core.cache import get_redis
from app.core.errors import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

_LUA_INCR_EXPIRE = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('TTL', KEYS[1])}
"""


def parse_rule(rule: str) -> tuple[int, int]:
    """'10/300' -> (10 requests, 300 seconds)."""
    limit, _, window = rule.partition("/")
    return int(limit), int(window)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce(bucket: str, identity: str, rule: str) -> None:
    """Raise RateLimitedError when `identity` exceeds `rule` in `bucket`.

    A Redis failure fails OPEN: losing the cache must not lock every customer
    out of logging in.
    """
    limit, window = parse_rule(rule)
    key = f"qs:rl:{bucket}:{identity}"
    try:
        current, ttl = await get_redis().eval(  # type: ignore[no-untyped-call]
            _LUA_INCR_EXPIRE, 1, key, window
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ratelimit.unavailable", bucket=bucket, error=str(exc))
        return

    if current > limit:
        logger.info("ratelimit.blocked", bucket=bucket, identity=identity, count=current)
        raise RateLimitedError(
            "Too many requests. Please wait and try again.",
            retry_after=max(int(ttl), 1),
        )


class RateLimit:
    """Dependency factory: `Depends(RateLimit("login", settings.rate_limit_login))`."""

    def __init__(self, bucket: str, rule: str):
        self.bucket = bucket
        self.rule = rule

    async def __call__(self, request: Request) -> None:
        await enforce(self.bucket, client_ip(request), self.rule)
