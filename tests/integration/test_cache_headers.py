"""What may be cached, and what must never be.

The measured problem: from Tashkent one API request costs ~350 ms of network
against 2–4 ms of work in the application. That ratio is not an application
problem — it is three handshakes across a continent — so the fix is to stop
asking for the same answer twice.

The risk that comes with it is the reason these tests exist. Marking the wrong
response public puts somebody's order, profile or eSIM QR code into a shared
cache, and that failure surfaces as the wrong customer seeing another
customer's purchase. So the default is closed and every exception is asserted
by name.
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


class TestCatalogueIsCacheable:
    async def test_it_is_public_with_a_revalidation_window(self, client: AsyncClient) -> None:
        response = await client.get("/api/regions")
        assert response.status_code == 200
        cache = response.headers["cache-control"]
        assert "public" in cache
        # stale-while-revalidate is the part that removes the wait: the browser
        # serves what it has and refreshes behind the visitor's back.
        assert "stale-while-revalidate" in cache

    async def test_it_varies_by_language(self, client: AsyncClient) -> None:
        """Otherwise a shared cache serves Uzbek prices to a Russian visitor.

        And it would do it at the edge, where we could not see it happening.
        """
        response = await client.get("/api/regions")
        assert "Accept-Language" in response.headers.get("vary", "")

    async def test_the_same_content_answers_304_with_no_body(self, client: AsyncClient) -> None:
        first = await client.get("/api/regions")
        etag = first.headers["etag"]

        again = await client.get("/api/regions", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""

    async def test_a_stale_etag_gets_the_full_answer(self, client: AsyncClient) -> None:
        response = await client.get("/api/regions", headers={"If-None-Match": 'W/"nonsense"'})
        assert response.status_code == 200
        assert response.content

    async def test_the_etag_follows_the_content(self, client: AsyncClient) -> None:
        """A version counter would have to be bumped by hand somewhere; a hash
        of the body cannot drift from what it describes."""
        uz = await client.get("/api/regions", headers={"Accept-Language": "uz"})
        ru = await client.get("/api/regions", headers={"Accept-Language": "ru"})
        if uz.content == ru.content:
            pytest.skip("regions are not translated in this dataset")
        assert uz.headers["etag"] != ru.headers["etag"]


class TestPrivateAnswersAreNeverCached:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/account/summary",
            "/api/account/esims",
            "/api/account/orders",
            "/api/auth/me",
        ],
    )
    async def test_they_are_marked_no_store(self, client: AsyncClient, path: str) -> None:
        """Asserted even on the 401: the header is what protects the answer, and
        an unauthenticated probe is exactly when a proxy might store it."""
        response = await client.get(path)
        assert response.headers["cache-control"] == "private, no-store"

    async def test_a_new_path_defaults_to_private(self, client: AsyncClient) -> None:
        """The list of public prefixes is a list of exceptions.

        A route added tomorrow is private until somebody says otherwise, which is
        the safe direction for a shop that serves QR codes.
        """
        response = await client.get("/api/definitely-not-a-route")
        assert response.headers["cache-control"] == "private, no-store"
