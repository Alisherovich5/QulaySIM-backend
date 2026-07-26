"""Authentication use cases.

Fixes carried over from the previous version:
  * register no longer leaks which e-mails exist (generic message + the unique
    index, not a check-then-insert race, decides the outcome);
  * referral attachment happens inside the same transaction as the insert;
  * referral-code generation is bounded instead of `while True`.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, ConflictError, DomainError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_revoked,
    revoke,
    verify_password,
)
from app.db.models import Customer, Referral
from app.domain.referral import new_referral_code, normalise_code
from app.repositories import customers as customer_repo

logger = get_logger(__name__)

MAX_CODE_ATTEMPTS = 8


async def _unique_referral_code(session: AsyncSession) -> str:
    for _ in range(MAX_CODE_ATTEMPTS):
        code = new_referral_code()
        if not await customer_repo.referral_code_exists(session, code):
            return code
    # 36^8 keyspace; this means something is badly wrong, not bad luck.
    raise DomainError("Could not allocate a referral code, please retry")


async def _attach_referrer(session: AsyncSession, customer: Customer, raw_code: str | None) -> None:
    code = normalise_code(raw_code)
    if not code:
        return
    referrer = await customer_repo.get_by_referral_code(session, code)
    if referrer is None or referrer.id == customer.id:
        logger.info("referral.code_ignored", code=code)
        return
    customer.referred_by_id = referrer.id
    session.add(
        Referral(
            referrer_id=referrer.id,
            referred_id=customer.id,
            referred_email=customer.email,
            status="pending",
        )
    )


async def register(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    password: str,
    referral_code: str | None,
) -> Customer:
    customer = Customer(
        email=email.strip().lower(),
        full_name=full_name,
        hashed_password=hash_password(password),
        referral_code=await _unique_referral_code(session),
    )
    session.add(customer)
    try:
        # Flush (not commit) so the customer gets an id and the unique index is
        # checked while the referral row can still join the same transaction.
        await session.flush()
        await _attach_referrer(session, customer, referral_code)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Deliberately identical wording regardless of which constraint fired.
        raise ConflictError("This e-mail cannot be registered") from None

    await session.refresh(customer)
    logger.info("auth.registered", customer_id=customer.id)
    return customer


async def authenticate(session: AsyncSession, *, email: str, password: str) -> Customer:
    customer = await customer_repo.get_by_email(session, email)
    # verify_password runs a bcrypt comparison even when `customer` is None,
    # so a missing account and a wrong password are indistinguishable by timing.
    valid = verify_password(password, customer.hashed_password if customer else None)
    if customer is None or not valid:
        logger.info("auth.login_failed", email_hint=email[:2])
        raise AuthenticationError("Incorrect email or password")
    if not customer.is_active:
        raise AuthenticationError("Account disabled")
    logger.info("auth.login_ok", customer_id=customer.id)
    return customer


def issue_tokens(customer: Customer) -> tuple[str, str, int]:
    from app.core.config import settings

    access = create_access_token(str(customer.id))
    refresh, _ = create_refresh_token(str(customer.id))
    return access, refresh, settings.access_token_ttl_minutes * 60


async def rotate_refresh_token(
    session: AsyncSession, refresh_token: str
) -> tuple[Customer, str, str, int]:
    """Single-use refresh tokens: the presented one is revoked as it is spent."""
    payload = decode_token(refresh_token, "refresh")
    jti = str(payload["jti"])
    if await is_revoked(jti):
        logger.warning("auth.refresh_reuse_detected", jti=jti)
        raise AuthenticationError("Session is no longer valid")

    customer = await customer_repo.get_by_id(session, int(payload["sub"]))
    if customer is None or not customer.is_active:
        raise AuthenticationError("Session is no longer valid")

    from app.core.config import settings

    await revoke(jti, settings.refresh_token_ttl_days * 86400)
    access, new_refresh, ttl = issue_tokens(customer)
    return customer, access, new_refresh, ttl


async def logout(refresh_token: str | None) -> None:
    if not refresh_token:
        return
    try:
        payload = decode_token(refresh_token, "refresh")
    except AuthenticationError:
        return
    from app.core.config import settings

    await revoke(str(payload["jti"]), settings.refresh_token_ttl_days * 86400)
