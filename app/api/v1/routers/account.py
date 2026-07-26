from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentCustomer, SessionDep
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
from app.schemas.commerce import ESIMOut, OrderOut, TopUpIn
from app.services import account as service

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/summary", response_model=AccountSummaryOut)
async def summary(session: SessionDep, customer: CurrentCustomer) -> JSONDict:
    return await service.summary(session, customer)


@router.get("/esims", response_model=list[ESIMOut])
async def my_esims(session: SessionDep, customer: CurrentCustomer) -> list[ESIM]:
    return await order_repo.list_esims(session, customer.id)


@router.get("/orders", response_model=list[OrderOut])
async def my_orders(session: SessionDep, customer: CurrentCustomer) -> list[Order]:
    return await order_repo.list_orders(session, customer.id)


@router.post("/esims/{esim_id}/activate", response_model=ESIMOut)
async def activate(esim_id: int, session: SessionDep, customer: CurrentCustomer) -> ESIM:
    return await service.activate_esim(session, customer, esim_id)


@router.post("/esims/{esim_id}/topup", response_model=ESIMOut)
async def topup(
    esim_id: int, payload: TopUpIn, session: SessionDep, customer: CurrentCustomer
) -> ESIM:
    return await service.topup_esim(session, customer, esim_id, payload.extra_mb)


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
