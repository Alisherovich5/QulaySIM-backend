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
        assert tokens["access_token"] and tokens["refresh_token"]

        me = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == email

        # Refresh rotates: the old token must not work twice.
        rotated = await client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert rotated.status_code == 200
        replay = await client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
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
