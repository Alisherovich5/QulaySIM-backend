"""Authentication use cases.

Fixes carried over from the previous version:
  * register no longer leaks which e-mails exist (generic message + the unique
    index, not a check-then-insert race, decides the outcome);
  * referral attachment happens inside the same transaction as the insert;
  * referral-code generation is bounded instead of `while True`.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    ServiceUnavailableError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_revoked,
    needs_rehash,
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

    # Opportunistic upgrade: the plaintext is only available here, so a hash
    # made at a weaker cost factor can be replaced at no cost to the customer.
    if needs_rehash(customer.hashed_password):
        customer.hashed_password = hash_password(password)
        await session.commit()
        logger.info("auth.password_rehashed", customer_id=customer.id)

    logger.info("auth.login_ok", customer_id=customer.id)
    return customer


async def login_with_google(session: AsyncSession, *, credential: str) -> Customer:
    """Sign in (or sign up) with a verified Google identity.

    Three cases, in this order, and the order is the security argument:

    1. **Known provider id** — the link already exists, so use it. Checked first
       because the provider's id is the only stable identifier; the address on
       the Google account may since have changed.
    2. **Unknown provider id, known e-mail** — link the two. Safe only because
       `verify_id_token` refuses tokens whose e-mail Google has not verified;
       without that check this branch would hand any account to whoever claimed
       its address at Google.
    3. **Neither** — create a passwordless customer. `hashed_password` stays
       empty and `verify_password` refuses an empty hash, so the account cannot
       later be entered with a guessed password.

    Case 2 is the one worth stating plainly: an address that already registered
    with a password is *linked*, not rejected and not duplicated. The fourth
    outcome is a conflict — the customer already holds a different Google link —
    which is refused rather than retried; see the bottom of the loop.
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.db.models import SocialAccount
    from app.integrations.google_auth import (
        GoogleAuthError,
        GoogleUnavailableError,
        verify_id_token,
    )

    try:
        identity = await verify_id_token(credential)
    except GoogleUnavailableError as exc:
        # Google being unreachable is not the customer's fault and must not be
        # reported as a bad account: 401 would tell them to fix something they
        # cannot, and would bury the outage in the ordinary login-failure rate.
        logger.warning("auth.google_unavailable", error=str(exc))
        raise ServiceUnavailableError(
            "Google sign-in is temporarily unavailable, please try again"
        ) from None
    except GoogleAuthError as exc:
        logger.info("auth.google_rejected", error=str(exc))
        raise AuthenticationError("Could not verify this Google account") from None

    # Two passes at most. The first can lose a race with another tab signing the
    # same person in for the first time; the second then resolves through case 1
    # above. This used to recurse instead, which was fine for a race but never
    # terminated for a conflict that is not one — see the second-attempt branch.
    for attempt in (1, 2):
        link = (
            await session.execute(
                select(SocialAccount).where(
                    SocialAccount.provider == "google",
                    SocialAccount.provider_uid == identity.subject,
                )
            )
        ).scalar_one_or_none()

        if link is not None:
            customer = await session.get(Customer, link.customer_id)
            if customer is None:  # pragma: no cover - FK makes this unreachable
                raise AuthenticationError("Account not found")
            if not customer.is_active:
                raise AuthenticationError("Account disabled")
            link.last_login_at = datetime.now(UTC)
            link.email = identity.email
            await session.commit()
            logger.info("auth.google_login", customer_id=customer.id, linked=True)
            return customer

        customer = await customer_repo.get_by_email(session, identity.email)
        created = customer is None
        if customer is None:
            customer = Customer(
                email=identity.email,
                full_name=identity.full_name,
                hashed_password="",
                referral_code=await _unique_referral_code(session),
            )
            session.add(customer)
            await session.flush()
        elif not customer.is_active:
            raise AuthenticationError("Account disabled")

        session.add(
            SocialAccount(
                customer_id=customer.id,
                provider="google",
                provider_uid=identity.subject,
                email=identity.email,
                last_login_at=datetime.now(UTC),
            )
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if attempt == 1:
                continue
            # Not a race, then. The standing case is Django's
            # `one_link_per_customer_per_provider`: this customer already holds
            # a Google link with a different `sub`, which happens when a Google
            # account is deleted and recreated on the same address. Retrying
            # can never clear that, so say so once instead of looping.
            logger.warning(
                "auth.google_link_conflict",
                email_hint=identity.email[:2],
                error=str(exc.orig),
            )
            raise ConflictError(
                "This e-mail is already linked to a different Google account"
            ) from None

        await session.refresh(customer)
        logger.info("auth.google_login", customer_id=customer.id, created=created)
        return customer

    # Unreachable: both passes either return or raise. Here for the type checker.
    raise ConflictError("Could not complete Google sign-in")


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
