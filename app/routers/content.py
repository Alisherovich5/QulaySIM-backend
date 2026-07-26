from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import FAQ, Banner, Benefit, Device, PromoBanner, Testimonial
from app.schemas import (
    BannerOut,
    BenefitOut,
    DeviceOut,
    FAQOut,
    LandingOut,
    PromoOut,
    TestimonialOut,
)

router = APIRouter(prefix="/api", tags=["content"])

SUPPORTED_LANGS = {"en", "ru", "uz"}


def loc(obj, field: str, lang: str) -> str:
    """Return the localized value of `field`, falling back to the English base."""
    if lang == "en":
        return getattr(obj, field)
    return getattr(obj, f"{field}_{lang}", "") or getattr(obj, field)


def _lang(value: str) -> str:
    return value if value in SUPPORTED_LANGS else "en"


def _faq_out(rows, lang: str) -> list[FAQOut]:
    return [
        FAQOut(
            id=r.id,
            question=loc(r, "question", lang),
            answer=loc(r, "answer", lang),
            category=r.category,
        )
        for r in rows
    ]


@router.get("/faqs", response_model=list[FAQOut])
def list_faqs(db: Session = Depends(get_db), lang: str = Query("en")):
    rows = (
        db.query(FAQ)
        .filter(FAQ.is_active == True)  # noqa: E712
        .order_by(FAQ.sort_order, FAQ.id)
        .all()
    )
    return _faq_out(rows, _lang(lang))


@router.get("/banners", response_model=list[BannerOut])
def list_banners(db: Session = Depends(get_db)):
    return (
        db.query(Banner)
        .filter(Banner.is_active == True)  # noqa: E712
        .order_by(Banner.sort_order, Banner.id)
        .all()
    )


@router.get("/content/landing", response_model=LandingOut)
def landing_content(db: Session = Depends(get_db), lang: str = Query("en")):
    """All admin-managed landing-page content, resolved to one language."""
    lang = _lang(lang)

    def active(model):
        query = db.query(model).filter(model.is_active == True)  # noqa: E712
        if model is Testimonial:
            query = query.filter(Testimonial.moderation_status == "approved")
        return query.order_by(model.sort_order, model.id).all()

    benefits = [
        BenefitOut(id=b.id, icon=b.icon, title=loc(b, "title", lang), text=loc(b, "text", lang))
        for b in active(Benefit)
    ]
    testimonials = [
        TestimonialOut(
            id=t.id,
            name=t.name,
            location=loc(t, "location", lang),
            text=loc(t, "text", lang),
            rating=t.rating,
        )
        for t in active(Testimonial)
    ]
    devices = [DeviceOut(id=d.id, name=d.name) for d in active(Device)]
    faqs = _faq_out(
        db.query(FAQ)
        .filter(FAQ.is_active == True)  # noqa: E712
        .order_by(FAQ.sort_order, FAQ.id)
        .all(),
        lang,
    )

    pb = (
        db.query(PromoBanner)
        .filter(PromoBanner.is_active == True)  # noqa: E712
        .order_by(PromoBanner.updated_at.desc())
        .first()
    )
    promo = (
        PromoOut(
            eyebrow=loc(pb, "eyebrow", lang),
            title=loc(pb, "title", lang),
            text=loc(pb, "text", lang),
            code=pb.code,
            cta_link=pb.cta_link,
        )
        if pb
        else None
    )

    return LandingOut(
        benefits=benefits,
        testimonials=testimonials,
        devices=devices,
        faqs=faqs,
        promo=promo,
    )
