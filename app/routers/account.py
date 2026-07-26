from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_customer, hash_password, verify_password
from app.models import ESIM, Country, Customer, Order, Plan, Referral, Testimonial
from app.schemas import (
    AccountSummaryOut,
    CustomerOut,
    ESIMOut,
    OrderOut,
    PassportCountry,
    ProfileUpdateIn,
    ReferralEntry,
    ReferralSummaryOut,
    TopUpIn,
    TestimonialStatusOut,
    TestimonialSubmitIn,
)
from app.services import referral as referral_service

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/testimonial", response_model=TestimonialStatusOut)
def testimonial_status(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    eligible = db.query(ESIM.id).filter(ESIM.customer_id == customer.id).first() is not None
    review = db.query(Testimonial).filter(Testimonial.customer_id == customer.id).first()
    return TestimonialStatusOut(
        eligible=eligible,
        status=review.moderation_status if review else None,
        rating=review.rating if review else None,
        location=review.location if review else None,
        text=review.text if review else None,
    )


@router.post("/testimonial", response_model=TestimonialStatusOut)
def submit_testimonial(
    payload: TestimonialSubmitIn,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    if db.query(ESIM.id).filter(ESIM.customer_id == customer.id).first() is None:
        raise HTTPException(status_code=403, detail="Only customers who purchased an eSIM can leave a review")

    review = db.query(Testimonial).filter(Testimonial.customer_id == customer.id).first()
    if review and review.moderation_status == "approved":
        raise HTTPException(status_code=409, detail="Your approved review is already published")

    if review is None:
        review = Testimonial(customer_id=customer.id)
        db.add(review)

    review.name = (customer.full_name.strip() or customer.email.split("@", 1)[0])[:80]
    review.location = payload.location.strip()
    review.text = payload.text.strip()
    review.rating = payload.rating
    review.location_ru = ""
    review.text_ru = ""
    review.location_uz = ""
    review.text_uz = ""
    review.moderation_status = "pending"
    review.is_active = True
    db.commit()
    db.refresh(review)
    return TestimonialStatusOut(
        eligible=True,
        status=review.moderation_status,
        rating=review.rating,
        location=review.location,
        text=review.text,
    )


@router.get("/orders", response_model=list[OrderOut])
def my_orders(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    return (
        db.query(Order)
        .options(joinedload(Order.esims).joinedload(ESIM.plan))
        .filter(Order.customer_id == customer.id)
        .order_by(Order.created_at.desc())
        .all()
    )


@router.get("/referrals", response_model=ReferralSummaryOut)
def my_referrals(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    code = referral_service.ensure_code(db, customer)
    rows = (
        db.query(Referral)
        .filter(Referral.referrer_id == customer.id)
        .order_by(Referral.created_at.desc())
        .all()
    )
    completed = [r for r in rows if r.status == "completed"]
    return ReferralSummaryOut(
        code=code,
        invited=len(rows),
        completed=len(completed),
        pending=len(rows) - len(completed),
        rewards=[r.reward_code for r in completed if r.reward_code],
        entries=[
            ReferralEntry(
                referred_email=r.referred_email,
                status=r.status,
                reward_code=r.reward_code,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


@router.get("/esims", response_model=list[ESIMOut])
def my_esims(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    return (
        db.query(ESIM)
        .options(joinedload(ESIM.plan))
        .filter(ESIM.customer_id == customer.id)
        .order_by(ESIM.created_at.desc())
        .all()
    )


@router.get("/summary", response_model=AccountSummaryOut)
def summary(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    esims = (
        db.query(ESIM)
        .options(joinedload(ESIM.plan).joinedload(Plan.country))
        .filter(ESIM.customer_id == customer.id)
        .all()
    )
    active = sum(1 for e in esims if e.status == "active")
    data_used = sum(e.data_used_mb for e in esims)
    data_total = sum(e.data_total_mb for e in esims)

    # Travel passport — distinct countries the customer has connected to.
    by_country: dict[str, PassportCountry] = {}
    for e in esims:
        c: Country | None = e.plan.country if e.plan else None
        if not c:
            continue
        if c.iso2 not in by_country:
            by_country[c.iso2] = PassportCountry(iso2=c.iso2, name=c.name, esims=0)
        by_country[c.iso2].esims += 1
    passport = sorted(by_country.values(), key=lambda p: -p.esims)

    total_spent = (
        db.query(func.coalesce(func.sum(Order.total), 0))
        .filter(Order.customer_id == customer.id, Order.status == "paid")
        .scalar()
    )
    orders_count = (
        db.query(func.count(Order.id)).filter(Order.customer_id == customer.id).scalar()
    )

    return AccountSummaryOut(
        full_name=customer.full_name,
        email=customer.email,
        member_since=customer.created_at,
        active_esims=active,
        total_esims=len(esims),
        data_used_mb=data_used,
        data_total_mb=data_total,
        countries_connected=len(by_country),
        total_spent=float(total_spent or 0),
        orders_count=orders_count or 0,
        passport=passport,
    )


@router.patch("/profile", response_model=CustomerOut)
def update_profile(
    payload: ProfileUpdateIn,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    if payload.full_name is not None:
        customer.full_name = payload.full_name.strip()

    if payload.new_password:
        if not payload.current_password or not verify_password(
            payload.current_password, customer.hashed_password
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        customer.hashed_password = hash_password(payload.new_password)

    db.commit()
    db.refresh(customer)
    return customer


@router.post("/esims/{esim_id}/activate", response_model=ESIMOut)
def activate_esim(
    esim_id: int,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    esim = db.get(ESIM, esim_id)
    if not esim or esim.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="eSIM not found")
    if esim.status == "expired":
        raise HTTPException(status_code=400, detail="eSIM has expired")
    if esim.status == "pending":
        esim.status = "active"
        esim.activated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(esim)
    return esim


@router.post("/esims/{esim_id}/topup", response_model=ESIMOut)
def topup_esim(
    esim_id: int,
    payload: TopUpIn,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Add data to an eSIM (mock add-on — no real charge)."""
    esim = db.get(ESIM, esim_id)
    if not esim or esim.customer_id != customer.id:
        raise HTTPException(status_code=404, detail="eSIM not found")
    if esim.status == "expired":
        raise HTTPException(status_code=400, detail="eSIM has expired")
    if esim.data_total_mb == 0:
        raise HTTPException(status_code=400, detail="Plan is already unlimited")
    esim.data_total_mb += payload.extra_mb
    db.commit()
    db.refresh(esim)
    return esim
