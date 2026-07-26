"""Landing content localisation and caching."""

from __future__ import annotations

import pytest

from app.db.session import SessionFactory
from app.services import content as service


@pytest.fixture
async def session():
    async with SessionFactory() as s:
        yield s
        await s.rollback()


@pytest.mark.parametrize("language", ["en", "ru", "uz"])
async def test_landing_shape_is_stable_across_languages(session, language: str) -> None:
    payload = await service.landing_content(session, language)
    # The storefront destructures all five keys; none may go missing.
    for key in ("benefits", "testimonials", "devices", "faqs", "promo"):
        assert key in payload


async def test_only_approved_testimonials_are_published(session) -> None:
    """A pending review must never reach the storefront."""
    from app.repositories.content import approved_testimonials

    for row in await approved_testimonials(session):
        assert row.moderation_status == "approved"
        assert row.is_active is True


async def test_second_call_is_served_from_cache(session) -> None:
    from app.core.cache import cache_key, get_redis

    await get_redis().delete(cache_key("landing", lang="en"))
    await service.landing_content(session, "en")
    assert await get_redis().exists(cache_key("landing", lang="en")) == 1
