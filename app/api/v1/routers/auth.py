from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentCustomer, SessionDep
from app.core.config import settings
from app.core.cookies import REFRESH_COOKIE, clear_refresh_cookie, set_refresh_cookie
from app.core.errors import AuthenticationError
from app.core.ratelimit import RateLimit, client_ip, enforce
from app.db.models import Customer
from app.schemas.auth import CustomerOut, RefreshIn, RegisterIn, TokenOut
from app.services import auth as auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _issue(response: Response, customer: Customer) -> TokenOut:
    """Access token in the body, refresh token in an httpOnly cookie.

    The refresh token is deliberately absent from the response body: if
    JavaScript can read it, so can an XSS, and it is worth 30 days of access.
    """
    access, refresh, ttl = auth_service.issue_tokens(customer)
    set_refresh_cookie(response, refresh)
    return TokenOut(access_token=access, expires_in=ttl)


@router.post(
    "/register",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimit("register", settings.rate_limit_register))],
)
async def register(payload: RegisterIn, session: SessionDep, response: Response) -> TokenOut:
    customer = await auth_service.register(
        session,
        email=payload.email,
        full_name=payload.full_name,
        password=payload.password,
        referral_code=payload.referral_code,
    )
    return _issue(response, customer)


@router.post("/login", response_model=TokenOut)
async def login(
    request: Request,
    response: Response,
    session: SessionDep,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenOut:
    # Limit per IP *and* per account, so one attacker cannot spray many
    # accounts from one host, nor many hosts at one account.
    await enforce("login_ip", client_ip(request), settings.rate_limit_login)
    await enforce("login_user", form.username.strip().lower(), settings.rate_limit_login)

    customer = await auth_service.authenticate(session, email=form.username, password=form.password)
    return _issue(response, customer)


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    session: SessionDep,
    response: Response,
    payload: RefreshIn | None = None,
    qs_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenOut:
    """Rotate the session.

    The cookie is the browser path. The request body is kept as a fallback for
    non-browser clients (mobile apps, integration tests) that hold no cookies.
    """
    token = qs_refresh or (payload.refresh_token if payload else None)
    if not token:
        raise AuthenticationError("No refresh token supplied")

    _, access, new_refresh, ttl = await auth_service.rotate_refresh_token(session, token)
    set_refresh_cookie(response, new_refresh)
    return TokenOut(access_token=access, expires_in=ttl)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    payload: RefreshIn | None = None,
    qs_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    await auth_service.logout(qs_refresh or (payload.refresh_token if payload else None))
    clear_refresh_cookie(response)


@router.get("/me", response_model=CustomerOut)
async def me(customer: CurrentCustomer) -> CustomerOut:
    return CustomerOut.model_validate(customer)
