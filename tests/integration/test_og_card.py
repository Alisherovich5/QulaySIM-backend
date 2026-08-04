"""The share card endpoint — what a Telegram unfurl actually receives."""

from __future__ import annotations

import io

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


async def _an_active_slug(client: AsyncClient) -> str:
    countries = (await client.get("/api/countries")).json()
    if not countries:
        pytest.skip("catalogue is empty; run scripts.seed")
    return countries[0]["slug"]


class TestOgCard:
    async def test_an_active_destination_gets_a_real_png(self, client: AsyncClient) -> None:
        slug = await _an_active_slug(client)
        response = await client.get(f"/api/og/{slug}.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        # The bytes, not just the header: a broken render served with the right
        # content type would still ruin every unfurl.
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        image = Image.open(io.BytesIO(response.content))
        assert image.size == (1200, 630)

    async def test_the_card_is_cacheable_for_a_day(self, client: AsyncClient) -> None:
        slug = await _an_active_slug(client)
        response = await client.get(f"/api/og/{slug}.png")
        assert response.headers["cache-control"] == "public, max-age=86400"

    async def test_a_second_request_serves_the_same_bytes(self, client: AsyncClient) -> None:
        # Byte-equality is the observable half of "the render was cached";
        # a re-render would at minimum re-run PNG encoding, and any drift in
        # inputs between the calls would show up here.
        slug = await _an_active_slug(client)
        first = await client.get(f"/api/og/{slug}.png")
        second = await client.get(f"/api/og/{slug}.png")
        assert first.content == second.content

    async def test_an_unknown_destination_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/og/definitely-not-a-country.png")
        assert response.status_code == 404

    async def test_a_hostile_slug_is_refused_not_rendered(self, client: AsyncClient) -> None:
        # The slug feeds a cache key; the path pattern keeps anything that is
        # not a plain slug out of Redis keyspace entirely.
        response = await client.get("/api/og/Turkey%20OR%201=1.png")
        assert response.status_code in (404, 422)
