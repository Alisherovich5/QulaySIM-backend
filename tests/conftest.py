from __future__ import annotations

import contextlib
import os

# Set before any app import: config.py refuses to load without these.
os.environ.setdefault("JWT_SECRET", "test-only-secret-value-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://fastsim:fastsim@127.0.0.1:5432/fastsim")
# Database 15 is the throwaway test keyspace — never share it with dev data.
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Tests must never reach Telegram. A developer's .env carries the real bot token,
# and the support-form smoke test posts a plausible-looking message — which is how
# "Test User / customer@example.com" arrived on the shop owner's phone during a
# routine test run.
#
# Set, not setdefault: the point is to override whatever the environment holds.
# With no token the endpoint answers 503 before any network call, which the smoke
# test already accepts. Tests that exercise delivery set the token themselves and
# patch the HTTP client.
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
# A fixed Fernet key so the encrypted columns work under test. Without it
# every test that creates an eSIM died on the QR payload — eleven of them,
# which read as a broken suite rather than a missing variable. Fixed rather
# than generated per run so a dump taken from one run is readable in the next.
os.environ.setdefault("FIELD_ENCRYPTION_KEY", "B9QnF8_weiSbBAef1ISDIwsI9s5eSOKy7fYRzyF6pSk=")

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
async def clean_redis():
    """Rate-limit counters and cached responses must not leak between tests.

    Without this the register limiter (5/hour) starts returning 429 partway
    through the suite — which is the limiter working correctly, but it makes
    every later assertion meaningless.
    """
    from app.core.cache import get_redis

    # A missing Redis is surfaced by the readiness test, not by every fixture.
    with contextlib.suppress(Exception):
        await get_redis().flushdb()
    yield
