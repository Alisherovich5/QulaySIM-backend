"""Distributed fixed-window rate limiting backed by Redis.

The previous implementation used an in-process dict: it did not survive a
restart, did not apply across workers, and grew without bound.
"""

from __future__ import annotations

import hashlib

from fastapi import Request

from app.core.cache import get_redis
from app.core.cloudflare_ips import is_cloudflare
from app.core.config import settings
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


# Where the visitor's address is written, and why it is not simply read off the
# socket.
#
# The chain is visitor → Caddy → this app, and Cloudflare adds one more link in
# front. Each proxy appends its own peer to X-Forwarded-For, so the header grows
# from the right and the entries our own proxies wrote are the last ones. The
# left-most entry is whatever the caller typed, which is why it is never used:
# reading from the left let anyone rotate a header and mint unlimited fresh
# buckets, which is the whole limit gone.
#
# The hop count therefore has to match reality, and it changed the day Cloudflare
# went in front — off by one, every visitor in the country shares one bucket and
# starts seeing 429s that have nothing to do with them.
def _peer(request: Request) -> str:
    """The address our own proxy heard from — the last hop before us.

    Caddy replaces X-Forwarded-For with the address it received the connection
    from rather than appending to it, measured on this deployment. So the
    right-most entry is the peer, and with Cloudflare in front that peer is a
    Cloudflare edge.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [part.strip() for part in forwarded.split(",") if part.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else ""


def client_ip(request: Request) -> str:
    """The address the rate limits are counted against."""
    # Cloudflare states the true client in its own header and does not let a
    # caller override it. Believed only when the request actually arrived
    # through Cloudflare: anyone who reaches the origin directly can send that
    # header themselves, and being believed would hand them the payment
    # callback's source-range check and somebody else's rate-limit bucket.
    if settings.trust_cloudflare_client_ip and is_cloudflare(_peer(request)):
        candidate = request.headers.get("cf-connecting-ip", "").strip()
        if _is_ip(candidate):
            return candidate

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        hops = [part.strip() for part in forwarded.split(",") if part.strip()]
        if hops:
            # One step back per proxy of ours, counting from the right.
            index = max(0, len(hops) - max(1, settings.trusted_proxy_hops))
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
    # The identity is hashed, not stored: the per-account login bucket keys on
    # an e-mail address, and a Redis snapshot or a `--scan` by anyone who
    # reaches the cache should not hand over a list of customer addresses. A
    # truncated SHA-256 keeps the bucket just as unique without holding the PII.
    key = f"qs:rl:{bucket}:{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
    try:
        current, ttl = await get_redis().eval(  # type: ignore[no-untyped-call]
            _LUA_INCR_EXPIRE, 1, key, window
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ratelimit.unavailable", bucket=bucket, error=str(exc))
        return

    if current > limit:
        # The bucket and count are what an operator acts on; the identity is
        # PII and the hashed key above is enough to correlate repeat offenders
        # across log lines without writing an address into them.
        logger.info("ratelimit.blocked", bucket=bucket, count=current)
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
