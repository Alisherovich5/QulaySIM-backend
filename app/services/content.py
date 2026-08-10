"""Landing-page CMS content, localised and cached."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_key, get_or_set, invalidate
from app.core.config import settings
from app.domain.localisation import localise
from app.repositories import content as repo
from app.schemas.base import JSONDict
from app.schemas.content import (
    BenefitOut,
    DeviceOut,
    FaqOut,
    LandingContentOut,
    PromoOut,
    TestimonialOut,
)


async def landing_content(session: AsyncSession, language: str) -> JSONDict:
    async def produce() -> JSONDict:
        benefits = await repo.active_benefits(session)
        testimonials = await repo.approved_testimonials(session)
        devices = await repo.active_devices(session)
        faqs = await repo.active_faqs(session)
        promo, promo_code = await repo.current_promo_banner(session)

        payload = LandingContentOut(
            benefits=[
                BenefitOut(
                    id=b.id,
                    icon=b.icon,
                    title=localise(b, "title", language),
                    text=localise(b, "text", language),
                )
                for b in benefits
            ],
            testimonials=[
                TestimonialOut(
                    id=t.id,
                    name=t.name,
                    location=localise(t, "location", language),
                    text=localise(t, "text", language),
                    rating=t.rating,
                )
                for t in testimonials
            ],
            devices=[DeviceOut(id=d.id, name=d.name) for d in devices],
            faqs=[
                FaqOut(
                    id=f.id,
                    question=localise(f, "question", language),
                    answer=localise(f, "answer", language),
                    category=f.category,
                )
                for f in faqs
            ],
            promo=(
                PromoOut(
                    eyebrow=localise(promo, "eyebrow", language),
                    title=localise(promo, "title", language),
                    text=localise(promo, "text", language).replace(
                        "{{code}}", promo_code.code if promo_code else promo.code
                    ),
                    # The linked code wins over the typed one: it is the code
                    # checkout will actually accept.
                    code=promo_code.code if promo_code else promo.code,
                    cta_link=promo.cta_link,
                    strip_text=localise(promo, "strip_text", language),
                    discount_type=promo_code.discount_type if promo_code else None,
                    discount_value=promo_code.discount_value if promo_code else None,
                    first_order_only=bool(promo_code.first_order_only) if promo_code else False,
                )
                if promo
                else None
            ),
        )
        return payload.model_dump(mode="json")

    return await get_or_set(
        cache_key("landing", lang=language), settings.cache_ttl_landing, produce
    )


async def invalidate_landing() -> int:
    return await invalidate("qs:landing*")
