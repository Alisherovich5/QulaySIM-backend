from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import CurrentCustomer, SessionDep, language_from
from app.core.errors import ValidationError
from app.db.models import ESIM, Order
from app.repositories import orders as order_repo
from app.schemas.account import (
    AccountSummaryOut,
    ReferralSummaryOut,
    TestimonialStatusOut,
    TestimonialSubmitIn,
)
from app.schemas.auth import CustomerOut, ProfileUpdateIn
from app.schemas.base import JSONDict
from app.schemas.commerce import ESIMOut, OrderOut
from app.services import account as service

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/summary", response_model=AccountSummaryOut)
async def summary(
    session: SessionDep,
    customer: CurrentCustomer,
    language: Annotated[str, Depends(language_from)],
) -> JSONDict:
    return await service.summary(session, customer, language=language)


@router.get("/esims", response_model=list[ESIMOut])
async def my_esims(session: SessionDep, customer: CurrentCustomer) -> list[ESIM]:
    return await order_repo.list_esims(session, customer.id)


@router.get("/orders", response_model=list[OrderOut])
async def my_orders(session: SessionDep, customer: CurrentCustomer) -> list[Order]:
    return await order_repo.list_orders(session, customer.id)


@router.post("/esims/{esim_id}/activate", response_model=ESIMOut)
async def activate(esim_id: int, session: SessionDep, customer: CurrentCustomer) -> ESIM:
    return await service.activate_esim(session, customer, esim_id)


# The top-up endpoint is deliberately absent.
#
# What it did was `esim.data_total_mb += extra_mb` and commit: no payment, no
# call to the supplier. So it was free — a customer could press the button until
# they held 50 GB — and the data was fictional, because only our own row changed
# while the profile on the wholesaler's side still carried what was bought. The
# customer would have been shown an allowance they could never use, which is the
# worse half of the two.
#
# A real top-up is a purchase: price it, take payment, order the extra bundle
# from the supplier, and only then move the number. Until that exists, no route.


@router.patch("/profile", response_model=CustomerOut)
async def update_profile(
    payload: ProfileUpdateIn, session: SessionDep, customer: CurrentCustomer
) -> CustomerOut:
    updated = await service.update_profile(
        session,
        customer,
        full_name=payload.full_name,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return CustomerOut.model_validate(updated)


@router.get("/referrals", response_model=ReferralSummaryOut)
async def referrals(session: SessionDep, customer: CurrentCustomer) -> JSONDict:
    return await service.referral_summary(session, customer)


@router.get("/testimonial", response_model=TestimonialStatusOut)
async def testimonial_status(session: SessionDep, customer: CurrentCustomer) -> JSONDict:
    return await service.testimonial_status(session, customer)


@router.post("/testimonial", response_model=TestimonialStatusOut)
async def submit_testimonial(
    payload: TestimonialSubmitIn, session: SessionDep, customer: CurrentCustomer
) -> JSONDict:
    return await service.submit_testimonial(
        session,
        customer,
        rating=payload.rating,
        location=payload.location,
        text=payload.text,
    )


@router.post("/avatar", response_model=AccountSummaryOut)
async def upload_avatar(
    session: SessionDep,
    customer: CurrentCustomer,
    file: Annotated[UploadFile, File(description="JPEG, PNG or WebP, up to 5 MB")],
) -> JSONDict:
    """Replace the customer's avatar.

    The declared content type is not consulted — it is trivially forged, and the
    only thing that establishes an upload is an image is decoding it. Reading is
    capped so an oversized body cannot be streamed into memory first.
    """
    from app.domain.avatars import MAX_UPLOAD_BYTES, AvatarRejectedError

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        await service.set_avatar(session, customer, raw)
    except AvatarRejectedError as rejected:
        # 422 with the rule's code, matching how password rules are reported, so
        # the storefront can translate the reason rather than show it in English.
        raise ValidationError(rejected.message, code=rejected.code) from None
    return await service.summary(session, customer)


@router.delete("/avatar", response_model=AccountSummaryOut)
async def delete_avatar(session: SessionDep, customer: CurrentCustomer) -> JSONDict:
    await service.clear_avatar(session, customer)
    return await service.summary(session, customer)
