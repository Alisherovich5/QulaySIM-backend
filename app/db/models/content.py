from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class FAQ(Base):
    __tablename__ = "content_faq"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(255))
    answer: Mapped[str] = mapped_column(Text)
    question_ru: Mapped[str] = mapped_column(String(255), default="")
    answer_ru: Mapped[str] = mapped_column(Text, default="")
    question_uz: Mapped[str] = mapped_column(String(255), default="")
    answer_uz: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(20), default="general")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Banner(Base):
    __tablename__ = "content_banner"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    subtitle: Mapped[str] = mapped_column(String(255), default="")
    image_url: Mapped[str] = mapped_column(String(200), default="")
    cta_text: Mapped[str] = mapped_column(String(60), default="")
    cta_link: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Benefit(Base):
    __tablename__ = "content_benefit"

    id: Mapped[int] = mapped_column(primary_key=True)
    icon: Mapped[str] = mapped_column(String(40), default="Globe2")
    title: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    title_ru: Mapped[str] = mapped_column(String(120), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    title_uz: Mapped[str] = mapped_column(String(120), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Testimonial(Base):
    __tablename__ = "content_testimonial"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers_customer.id"), unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(80))
    location: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    location_ru: Mapped[str] = mapped_column(String(120), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    location_uz: Mapped[str] = mapped_column(String(120), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    rating: Mapped[int] = mapped_column(Integer, default=5)
    moderation_status: Mapped[str] = mapped_column(String(10), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Device(Base):
    __tablename__ = "content_device"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoBanner(Base):
    __tablename__ = "content_promobanner"

    id: Mapped[int] = mapped_column(primary_key=True)
    eyebrow: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    eyebrow_ru: Mapped[str] = mapped_column(String(80), default="")
    title_ru: Mapped[str] = mapped_column(String(160), default="")
    text_ru: Mapped[str] = mapped_column(Text, default="")
    eyebrow_uz: Mapped[str] = mapped_column(String(80), default="")
    title_uz: Mapped[str] = mapped_column(String(160), default="")
    text_uz: Mapped[str] = mapped_column(Text, default="")
    code: Mapped[str] = mapped_column(String(40), default="WELCOME10")
    promo_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders_promocode.id"), nullable=True
    )
    strip_text: Mapped[str] = mapped_column(String(60), default="")
    strip_text_ru: Mapped[str] = mapped_column(String(60), default="")
    strip_text_uz: Mapped[str] = mapped_column(String(60), default="")
    cta_link: Mapped[str] = mapped_column(String(255), default="/destinations")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
