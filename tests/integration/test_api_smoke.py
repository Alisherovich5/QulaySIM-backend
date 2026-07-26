"""End-to-end checks against a real Postgres and Redis."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


class TestHealth:
    async def test_liveness(self, client: AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_reports_dependencies(self, client: AsyncClient) -> None:
        response = await client.get("/api/health/ready")
        body = response.json()
        assert "database" in body["checks"]
        assert "redis" in body["checks"]


class TestCatalogue:
    async def test_regions(self, client: AsyncClient) -> None:
        assert (await client.get("/api/regions")).status_code == 200

    async def test_countries(self, client: AsyncClient) -> None:
        assert (await client.get("/api/countries")).status_code == 200

    async def test_unknown_country_is_404_with_error_envelope(self, client: AsyncClient) -> None:
        response = await client.get("/api/countries/definitely-not-a-country")
        assert response.status_code == 404
        body = response.json()
        # The storefront reads error.response.data.detail — keep that key.
        assert body["code"] == "not_found"
        assert "detail" in body
        assert body["request_id"]


class TestAuthGuards:
    @pytest.mark.parametrize(
        "path", ["/api/account/summary", "/api/account/esims", "/api/account/orders"]
    )
    async def test_account_requires_a_token(self, client: AsyncClient, path: str) -> None:
        response = await client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_garbage_token_rejected(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/account/summary", headers={"Authorization": "Bearer nonsense"}
        )
        assert response.status_code == 401


class TestRegistrationAndLogin:
    async def test_full_cycle(self, client: AsyncClient) -> None:
        email = f"test-{uuid.uuid4().hex[:12]}@example.com"

        created = await client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "Test User", "password": "sufficiently-long-pw"},
        )
        assert created.status_code == 201
        tokens = created.json()
        assert tokens["access_token"]
        # The refresh token must never appear in a body a script can read.
        assert "refresh_token" not in tokens
        assert "qs_refresh" in created.cookies

        me = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == email

        # The client (httpx) now holds the cookie; refresh needs no body.
        stale = created.cookies["qs_refresh"]
        rotated = await client.post("/api/auth/refresh", json={})
        assert rotated.status_code == 200

        # Refresh tokens are single-use. The replay needs a cookie-less client:
        # the router prefers the cookie, which this one has already rotated.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bare:
            replay = await bare.post("/api/auth/refresh", json={"refresh_token": stale})
        assert replay.status_code == 401, "a spent refresh token must be rejected"

    async def test_duplicate_email_does_not_confirm_existence(self, client: AsyncClient) -> None:
        email = f"dupe-{uuid.uuid4().hex[:12]}@example.com"
        payload = {"email": email, "full_name": "A", "password": "sufficiently-long-pw"}
        assert (await client.post("/api/auth/register", json=payload)).status_code == 201

        second = await client.post("/api/auth/register", json=payload)
        assert second.status_code == 409
        # Must not say "already registered" — that is an enumeration oracle.
        assert "already" not in second.json()["detail"].lower()

    async def test_short_password_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/auth/register",
            json={"email": "x@example.com", "full_name": "A", "password": "short"},
        )
        assert response.status_code == 422


class TestQuote:
    async def test_empty_cart_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/checkout/quote", json={"items": []})
        assert response.status_code == 422

    async def test_absurd_quantity_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/checkout/quote", json={"items": [{"plan_id": 1, "quantity": 9999}]}
        )
        assert response.status_code == 422


class TestSecurityHeaders:
    async def test_headers_present(self, client: AsyncClient) -> None:
        headers = (await client.get("/api/health")).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-request-id"]

    async def test_request_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/api/health", headers={"X-Request-ID": "abc123"})
        assert response.headers["x-request-id"] == "abc123"


class TestWebhookAuth:
    async def test_missing_token_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/webhooks/esimaccess", json={})
        assert response.status_code == 401


class TestSupportContract:
    """The support form payload is defined by the storefront (Support.tsx).

    Regression guard: the schema was once changed to a `contact` field, which
    made every message from the real form fail validation.
    """

    async def test_accepts_the_storefront_payload(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/support/message",
            json={
                "name": "Test User",
                "email": "customer@example.com",
                "phone": "+998 90 123 45 67",
                "message": "This mirrors exactly what the support form sends.",
                "locale": "uz",
            },
        )
        # 202 when Telegram is configured, 503 when it is not — never 422.
        assert response.status_code in (202, 503), response.text

    async def test_rejects_a_malformed_phone(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/support/message",
            json={
                "name": "Test User",
                "email": "customer@example.com",
                "phone": "998901234567",
                "message": "Unformatted phone number should be rejected.",
                "locale": "uz",
            },
        )
        assert response.status_code == 422


class TestRefreshCookie:
    """The long-lived credential must be unreachable from JavaScript."""

    async def test_cookie_is_httponly_and_scoped(self, client: AsyncClient) -> None:
        email = f"cookie-{uuid.uuid4().hex[:12]}@example.com"
        response = await client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "C", "password": "sufficiently-long-pw"},
        )
        assert response.status_code == 201

        raw = response.headers["set-cookie"].lower()
        assert "httponly" in raw, "an XSS could otherwise read 30 days of access"
        assert "path=/api/auth" in raw, "the cookie should not ride on every request"
        assert "max-age=" in raw

    async def test_refresh_without_a_cookie_or_body_is_rejected(self, client: AsyncClient) -> None:
        fresh = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        async with fresh:
            assert (await fresh.post("/api/auth/refresh", json={})).status_code == 401

    async def test_logout_clears_the_cookie_and_revokes_the_token(
        self, client: AsyncClient
    ) -> None:
        email = f"logout-{uuid.uuid4().hex[:12]}@example.com"
        await client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "L", "password": "sufficiently-long-pw"},
        )
        assert (await client.post("/api/auth/logout", json={})).status_code == 204
        # The revoked token is refused even though httpx still replays a cookie.
        assert (await client.post("/api/auth/refresh", json={})).status_code == 401


class TestResponseHardening:
    async def test_csp_denies_everything(self, client: AsyncClient) -> None:
        """This API returns only JSON, so a stray HTML page must not run script."""
        csp = (await client.get("/api/health")).headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    async def test_session_responses_are_not_cacheable(self, client: AsyncClient) -> None:
        email = f"nocache-{uuid.uuid4().hex[:12]}@example.com"
        response = await client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "N", "password": "sufficiently-long-pw"},
        )
        assert response.headers.get("cache-control") == "no-store"


class TestMarginIsNotPublic:
    """Supplier cost and markup are commercially sensitive.

    They are mapped on the ORM model so workers and reports can use them, which
    makes it easy to leak them by adding a field to a response schema. These
    tests fail the moment that happens.
    """

    LEAKY_FIELDS = ("cost_usd", "markup_percent", "price_locked", "margin")

    async def test_country_detail_exposes_no_cost(self, client: AsyncClient) -> None:
        countries = (await client.get("/api/countries?limit=1")).json()
        if not countries:
            pytest.skip("catalogue is empty; run scripts.seed")

        body = (await client.get(f"/api/countries/{countries[0]['slug']}")).text
        for field in self.LEAKY_FIELDS:
            assert field not in body, f"{field} leaked in the country detail payload"

    async def test_country_list_exposes_no_cost(self, client: AsyncClient) -> None:
        body = (await client.get("/api/countries")).text
        for field in self.LEAKY_FIELDS:
            assert field not in body, f"{field} leaked in the country list payload"

    async def test_quote_exposes_no_cost(self, client: AsyncClient) -> None:
        countries = (await client.get("/api/countries?limit=1")).json()
        if not countries:
            pytest.skip("catalogue is empty; run scripts.seed")
        detail = (await client.get(f"/api/countries/{countries[0]['slug']}")).json()
        if not detail["plans"]:
            pytest.skip("no plans on the first country")

        response = await client.post(
            "/api/checkout/quote",
            json={"items": [{"plan_id": detail["plans"][0]["id"], "quantity": 1}]},
        )
        for field in self.LEAKY_FIELDS:
            assert field not in response.text, f"{field} leaked in the quote payload"
