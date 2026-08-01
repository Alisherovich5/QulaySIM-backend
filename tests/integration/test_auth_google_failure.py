"""The browser-side failure report for Google sign-in.

When Google Identity Services refuses, nothing reaches this service — the flow
dies in the browser and the access log stays empty. POST /api/auth/google/failed
exists only so that failure leaves a line in the log. These tests hold it to
what makes that safe to expose unauthenticated: a fixed vocabulary, a rate
limit, no storage, and a log line that names the fault and no one else.
"""

from __future__ import annotations

import contextlib
from typing import get_args

import pytest
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from app.core.config import settings
from app.core.ratelimit import parse_rule
from app.main import app
from app.schemas.auth import GoogleFailureReason

ENDPOINT = "/api/auth/google/failed"
EVENT = "auth.google_client_failed"

REASONS = list(get_args(GoogleFailureReason))


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def redis_is_up() -> bool:
    """The limiter fails open when Redis is gone, so a 429 cannot be asserted
    without it. Skipping is honest; passing on a fail-open would not be."""
    from app.core.cache import get_redis

    with contextlib.suppress(Exception):
        return bool(await get_redis().ping())
    return False


@pytest.mark.parametrize("reason", REASONS)
async def test_accepted_reason_returns_204_with_no_body(client: AsyncClient, reason: str) -> None:
    response = await client.post(ENDPOINT, json={"reason": reason})
    assert response.status_code == 204
    assert response.content == b""


async def test_log_line_carries_the_reason_and_nothing_else(client: AsyncClient) -> None:
    """Exact equality on purpose: this endpoint is the one place an anonymous
    caller can cause a log write, so the line must not grow a field that ties
    it to a person."""
    with capture_logs() as logs:
        response = await client.post(ENDPOINT, json={"reason": "button_not_rendered"})

    assert response.status_code == 204
    assert [entry for entry in logs if entry["event"] == EVENT] == [
        {"event": EVENT, "log_level": "warning", "reason": "button_not_rendered"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "not-a-real-reason"},
        {"reason": "button_not_rendered; DROP TABLE"},
        {"reason": ""},
        {"reason": "x" * 4096},
        {"reason": None},
        {"reason": ["button_not_rendered"]},
        {},
    ],
    ids=["unknown-code", "injection", "empty", "huge", "null", "list", "missing"],
)
async def test_reason_off_the_allow_list_is_rejected_and_never_logged(
    client: AsyncClient, body: dict
) -> None:
    with capture_logs() as logs:
        response = await client.post(ENDPOINT, json=body)

    assert response.status_code == 422
    # The point of the allow-list: caller-chosen text must not reach the log.
    assert not [entry for entry in logs if entry["event"] == EVENT]


async def test_it_is_rate_limited_like_the_sign_in_it_reports_on(client: AsyncClient) -> None:
    if not await redis_is_up():
        pytest.skip("Redis unavailable; the limiter fails open by design")

    limit, _ = parse_rule(settings.rate_limit_login)

    # capture_logs also keeps the flood out of the test output.
    with capture_logs() as logs:
        codes = [
            (await client.post(ENDPOINT, json={"reason": "popup_closed"})).status_code
            for _ in range(limit + 1)
        ]

    assert codes[:limit] == [204] * limit
    assert codes[-1] == 429
    # The limiter runs as a dependency, so a blocked report writes no line —
    # one caller cannot flood the log through this endpoint.
    assert len([entry for entry in logs if entry["event"] == EVENT]) == limit
