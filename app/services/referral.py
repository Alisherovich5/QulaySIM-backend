"""Referral program helpers — code generation and reward granting."""
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Customer, Order, PromoCode, Referral

ALPHABET = string.ascii_uppercase + string.digits
REWARD_PERCENT = 10  # referrer gets a single-use 10% code when an invitee buys


def generate_code(db: Session, length: int = 8) -> str:
    """Return a referral code not already used by any customer."""
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if not db.query(Customer).filter(Customer.referral_code == code).first():
            return code


def ensure_code(db: Session, customer: Customer) -> str:
    """Lazily assign a referral code to an existing customer."""
    if not customer.referral_code:
        customer.referral_code = generate_code(db)
        db.commit()
        db.refresh(customer)
    return customer.referral_code


def attach_referrer(db: Session, new_customer: Customer, referral_code: str | None) -> None:
    """Link a freshly-registered customer to whoever referred them and open a
    pending Referral record. No-op for invalid/self codes."""
    if not referral_code:
        return
    referrer = (
        db.query(Customer)
        .filter(Customer.referral_code == referral_code.strip().upper())
        .first()
    )
    if not referrer or referrer.id == new_customer.id:
        return
    new_customer.referred_by_id = referrer.id
    db.add(
        Referral(
            referrer_id=referrer.id,
            referred_id=new_customer.id,
            referred_email=new_customer.email,
            status="pending",
        )
    )
    db.commit()


def reward_on_first_order(db: Session, customer: Customer) -> None:
    """When a referred customer completes their first paid order, mark the
    referral completed and grant the referrer a single-use discount code."""
    if not customer.referred_by_id:
        return
    paid_orders = (
        db.query(Order)
        .filter(Order.customer_id == customer.id, Order.status == "paid")
        .count()
    )
    if paid_orders != 1:  # only the very first paid order triggers the reward
        return
    referral = (
        db.query(Referral)
        .filter(Referral.referred_id == customer.id, Referral.status == "pending")
        .first()
    )
    if not referral:
        return

    reward_code = "REF-" + "".join(secrets.choice(ALPHABET) for _ in range(6))
    db.add(
        PromoCode(
            code=reward_code,
            discount_type="percent",
            discount_value=REWARD_PERCENT,
            max_uses=1,
            used_count=0,
            is_active=True,
        )
    )
    referral.status = "completed"
    referral.reward_code = reward_code
    referral.completed_at = datetime.now(timezone.utc)
    db.commit()
