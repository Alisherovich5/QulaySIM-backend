from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    get_current_customer,
    hash_password,
    verify_password,
)
from app.models import Customer
from app.schemas import CustomerOut, RegisterIn, TokenOut
from app.services import referral as referral_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    existing = db.query(Customer).filter(Customer.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    customer = Customer(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        referral_code=referral_service.generate_code(db),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    # Link to whoever invited them (if a valid code was supplied).
    referral_service.attach_referrer(db, customer, payload.referral_code)
    return TokenOut(access_token=create_access_token(str(customer.id)))


@router.post("/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.email == form.username).first()
    if not customer or not verify_password(form.password, customer.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not customer.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    return TokenOut(access_token=create_access_token(str(customer.id)))


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(get_current_customer)):
    return customer
