"""Field measurement: what the site felt like to a real phone, in aggregate.

The reason this exists at all: Lighthouse on a developer's laptop measures a
laptop. The customer here is on a mid-range Android on 4G in Tashkent, three
handshakes from a server in Europe, on a route that loses packets in bursts. The
targets in the plan are p75 figures, and a p75 can only come from real visits.

The reason it is written carefully: this code runs in the browser of somebody
trying to buy something. It must never be able to break their page, and it must
never collect anything about them.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestCollecting:
    async def test_a_sample_is_accepted_and_answers_nothing(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/rum", json={"metric": "LCP", "value": 1650, "route": "country", "phone": True}
        )
        assert response.status_code == 204
        assert response.content == b""

    async def test_it_reaches_the_summary(self, client: AsyncClient) -> None:
        await client.post("/api/rum", json={"metric": "FCP", "value": 400, "route": "home"})
        summary = (await client.get("/api/rum/summary")).json()
        assert "FCP.desktop" in summary
        assert summary["FCP.desktop"]["samples"] >= 1

    async def test_phone_and_desktop_are_counted_apart(self, client: AsyncClient) -> None:
        """A fast laptop average hides a slow phone, and the phone is the customer."""
        await client.post("/api/rum", json={"metric": "INP", "value": 90, "route": "home"})
        await client.post(
            "/api/rum", json={"metric": "INP", "value": 4000, "route": "home", "phone": True}
        )
        summary = (await client.get("/api/rum/summary")).json()
        assert summary["INP.phone"]["p75"] != summary["INP.desktop"]["p75"]


class TestItCannotBecomeAProblem:
    async def test_an_unknown_metric_is_refused(self, client: AsyncClient) -> None:
        response = await client.post("/api/rum", json={"metric": "MADE_UP", "value": 1})
        assert response.status_code == 422

    async def test_a_route_cannot_carry_a_query_string(self, client: AsyncClient) -> None:
        """A full URL would bring query parameters, and those carry things a
        performance sample has no business keeping."""
        response = await client.post(
            "/api/rum", json={"metric": "LCP", "value": 1, "route": "/x?token=abc"}
        )
        assert response.status_code == 422

    async def test_an_absurd_value_is_refused(self, client: AsyncClient) -> None:
        response = await client.post("/api/rum", json={"metric": "LCP", "value": 10**9})
        assert response.status_code == 422

    async def test_extra_identifying_fields_are_ignored(self, client: AsyncClient) -> None:
        """Nothing about the visitor is accepted, even when offered.

        A sample that knew who it came from would be a privacy problem we could
        not undo later.
        """
        response = await client.post(
            "/api/rum",
            json={
                "metric": "CLS",
                "value": 0.05,
                "route": "home",
                "email": "someone@example.com",
                "user_id": 7,
            },
        )
        assert response.status_code == 204

    async def test_it_is_not_in_the_public_schema(self, client: AsyncClient) -> None:
        """Not a product endpoint; keeping it out of the docs keeps it from
        looking like one."""
        schema = (await client.get("/openapi.json")).json()
        assert "/api/rum" not in schema.get("paths", {})
