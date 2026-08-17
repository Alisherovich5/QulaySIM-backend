"""Real user measurements — what the site actually felt like, to real phones.

Lighthouse on a developer's laptop measures a laptop. The audience here is
someone on a mid-range Android on 4G in Tashkent, three handshakes away from a
server in Europe, on a route that loses packets in bursts. The only honest
number for that visitor is the one their own browser reports.

Deliberately small: a counter and a percentile need nothing more than the metric
name, its value, and enough context to tell a phone from a laptop. No identifier
of any kind is accepted or stored — a performance sample does not need to know
who it came from, and one that did would be a privacy problem we cannot fix
later.

Nothing is written to Postgres. Samples arrive constantly and matter in
aggregate, so they go into Redis: a rolling counter and a bucketed histogram per
metric per day, which is all a p75 needs.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from app.core.cache import get_redis
from app.core.config import settings
from app.core.logging import get_logger
from app.core.ratelimit import RateLimit

router = APIRouter(prefix="/api", tags=["rum"])
logger = get_logger("rum")

#: Kept for a fortnight — long enough to see whether a release made things
#: worse, short enough that nothing accumulates unwatched.
_TTL = 14 * 24 * 3600

#: Bucket edges in milliseconds, chosen around the thresholds that matter:
#: 1800 is the LCP target, 2500 is where Google stops calling it good.
_EDGES = (200, 500, 800, 1200, 1800, 2500, 4000, 6000, 10000)


class Sample(BaseModel):
    """One metric from one page view."""

    metric: Literal["LCP", "INP", "CLS", "TTFB", "FCP"]
    # CLS is a unitless ratio, the rest are milliseconds; both fit here and the
    # bucket edges are only meaningful per metric anyway.
    value: float = Field(ge=0, le=600_000)
    # Which page, coarsely. A full URL would carry query strings, and those
    # carry things a performance sample has no business keeping.
    route: str = Field(max_length=64, pattern=r"^[a-z0-9/_:-]*$")
    phone: bool = False


def _bucket(value: float) -> str:
    for edge in _EDGES:
        if value <= edge:
            return str(edge)
    return "over"


@router.post(
    "/rum",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(RateLimit("rum", settings.rate_limit_rum))],
    include_in_schema=False,
)
async def collect(sample: Sample, request: Request) -> Response:
    """Record one sample. Answers 204 whatever happens.

    A measurement endpoint that can fail a page load is worse than no
    measurement: this runs in the visitor's browser while they are trying to buy
    something. Every error is swallowed and logged on our side.
    """
    day = request.headers.get("x-date-override") if settings.debug else None
    try:
        redis = get_redis()
        # UTC day, taken from Redis rather than the process, so several API
        # containers agree on which day a sample belongs to.
        if not day:
            seconds = (await redis.time())[0]
            day = str(int(seconds) // 86400)
        device = "phone" if sample.phone else "desktop"
        key = f"qs:rum:{day}:{sample.metric}:{device}"
        pipe = redis.pipeline()
        pipe.hincrby(key, _bucket(sample.value), 1)
        pipe.hincrby(key, "n", 1)
        pipe.expire(key, _TTL)
        # Routes are counted separately and only by name, so a slow page can be
        # told from a slow site.
        if sample.route:
            route_key = f"qs:rum:{day}:{sample.metric}:route"
            pipe.hincrby(route_key, sample.route[:64], 1)
            pipe.expire(route_key, _TTL)
        await pipe.execute()
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning("rum.store_failed", metric=sample.metric)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/rum/summary", include_in_schema=False)
async def summary(
    days: Annotated[int, Field(ge=1, le=14)] = 7,
) -> dict[str, dict[str, float | int]]:
    """p75 per metric per device, which is the number the targets are set on.

    An average hides the tail, and the tail is the visitor who leaves. Open
    rather than authenticated: it exposes nothing but timings, and it is what
    makes the budget checkable by anyone who cares to look.
    """
    redis = get_redis()
    seconds = (await redis.time())[0]
    today = int(seconds) // 86400
    out: dict[str, dict[str, float | int]] = {}
    for metric in ("LCP", "INP", "CLS", "TTFB", "FCP"):
        for device in ("phone", "desktop"):
            counts: dict[str, int] = {}
            for offset in range(days):
                raw = await redis.hgetall(f"qs:rum:{today - offset}:{metric}:{device}")
                for field, value in raw.items():
                    name = field.decode()
                    counts[name] = counts.get(name, 0) + int(value)
            total = counts.pop("n", 0)
            if not total:
                continue
            # The p75 lands in the first bucket whose cumulative share passes
            # 75%; reporting the edge is honest about the resolution.
            target = total * 0.75
            seen = 0
            p75: float | str = "over"
            for edge in (*_EDGES, "over"):
                seen += counts.get(str(edge), 0)
                if seen >= target:
                    p75 = edge if isinstance(edge, int) else "over"
                    break
            out[f"{metric}.{device}"] = {"samples": total, "p75": p75}
    return out


class ClientError(BaseModel):
    """A crash that happened in a customer's browser."""

    # The message and the top of the stack are enough to find the bug; a full
    # stack from a minified bundle is mostly noise, and a long one is a place
    # for personal data to hide.
    message: str = Field(max_length=300)
    source: str = Field(default="", max_length=200)
    route: str = Field(default="", max_length=64, pattern=r"^[a-z0-9/_:-]*$")
    # Which build it came from, so a fixed bug stops being counted.
    build: str = Field(default="", max_length=40)


@router.post(
    "/client-errors",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(RateLimit("client_errors", settings.rate_limit_rum))],
    include_in_schema=False,
)
async def client_error(report: ClientError) -> Response:
    """Record a browser-side crash.

    Deliberately not the Sentry browser SDK. That is ~30 KB on every page load,
    on a connection where a round trip costs 350 ms, to catch an event that
    happens to a small fraction of visitors — and the size budget the same review
    asked for would have been spent on it. This costs a few hundred bytes and
    reports the same three things that actually get a bug fixed.

    Masked through the same rule as the server logs: a message string is exactly
    where an email address or a token ends up by accident.
    """
    from app.core.logging import _mask

    safe = _mask(None, "", report.model_dump())
    # Logged at error level so it lands wherever the server's errors land —
    # including Sentry, when a DSN is configured, without a second SDK.
    logger.error("client.error", **safe)
    try:
        redis = get_redis()
        seconds = (await redis.time())[0]
        day = int(seconds) // 86400
        key = f"qs:client_errors:{day}"
        pipe = redis.pipeline()
        pipe.hincrby(key, f"{safe.get('message', '')[:120]} @ {safe.get('route', '')}", 1)
        pipe.expire(key, _TTL)
        await pipe.execute()
    except Exception:  # noqa: BLE001
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
