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


# One hop in front of the app: nginx in the storefront image, which proxies to
# us with `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`. If Caddy
# or another proxy is ever inserted between them, raise this to match, or every
# limit below starts keying on an address the caller picked.
_TRUSTED_PROXY_HOPS = 1


def client_ip(request: Request) -> str:
    """The address the rate limits are counted against.

    Read from the RIGHT of X-Forwarded-For, not the left. nginx *appends* the
    real peer to whatever the caller sent, so the left-most entry is whatever
    the caller typed — taking it let anyone rotate a header and get an unlimited
    number of fresh buckets, which is the whole limit gone. The right-most
    entries are the ones our own proxies wrote, and only those can be trusted.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        hops = [part.strip() for part in forwarded.split(",") if part.strip()]
        if hops:
            # The last hop is the peer nginx saw. With more proxies in front,
            # step back one per trusted hop.
            index = max(0, len(hops) - _TRUSTED_PROXY_HOPS)
            candidate = hops[index] if index < len(hops) else hops[-1]
            # Never let a caller's text become a Redis key or a log field.
            if _is_ip(candidate):
                return candidate
    return request.client.host if request.client else "unknown"


def _is_ip(value: str) -> bool:
    from ipaddress import ip_address

    try:
        ip_address(value)
    except ValueError:
        return False
    return True


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
        # `identity` is an address or an e-mail the caller supplied; both are
        # bounded and validated upstream, so neither can smuggle a log line.
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
