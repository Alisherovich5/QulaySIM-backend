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


def language_from(request: Request, lang: str | None = None) -> str:
    """Query parameter wins; otherwise fall back to Accept-Language."""
    from app.domain.localisation import normalise_language

    if lang:
        return normalise_language(lang)
    header = request.headers.get("accept-language", "")
    return normalise_language(header.split(",")[0] if header else None)
