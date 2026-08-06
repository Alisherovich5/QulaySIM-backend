"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError
from app.core.security import decode_token
from app.db.models import Customer
from app.db.session import get_session
from app.repositories import customers as customer_repo

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_customer(
    session: SessionDep,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Customer:
    if not token:
        raise AuthenticationError("Not authenticated")
    payload = decode_token(token, "access")
    customer = await customer_repo.get_by_id(session, int(payload["sub"]))
    if customer is None or not customer.is_active:
        raise AuthenticationError("Could not validate credentials")
    return customer


CurrentCustomer = Annotated[Customer, Depends(get_current_customer)]


async def get_optional_customer(
    session: SessionDep,
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> Customer | None:
    """The signed-in customer, or None — never an error.

    For endpoints an anonymous visitor may also use, where knowing who is asking
    changes the answer. Quoting a cart is the case: anybody can price one, but a
    cashback code belongs to the person who earned it, so the quote has to know
    whether that person is the one asking.

    A bad or expired token yields None rather than a 401: the visitor is simply
    treated as anonymous, and a browser holding a stale token still gets a price
    instead of an error on a page that never required signing in.
    """
    if not token:
        return None
    try:
        payload = decode_token(token, "access")
    except Exception:  # noqa: BLE001 - any decode failure means "anonymous"
        # Deliberately broad. Whatever went wrong with the token — expired,
        # forged, malformed, signed with a rotated key — the answer for an
        # endpoint that does not require signing in is the same: treat the
        # visitor as anonymous and price their cart. Enumerating the failure
        # modes would add ways to get a 500 on a public page, not safety.
        return None
    customer = await customer_repo.get_by_id(session, int(payload["sub"]))
    return customer if customer is not None and customer.is_active else None


OptionalCustomer = Annotated[Customer | None, Depends(get_optional_customer)]


def language_from(request: Request, lang: str | None = None) -> str:
    """Query parameter wins; otherwise fall back to Accept-Language."""
    from app.domain.localisation import normalise_language

    if lang:
        return normalise_language(lang)
    header = request.headers.get("accept-language", "")
    return normalise_language(header.split(",")[0] if header else None)
