from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentCustomer, SessionDep
from app.core.config import settings
from app.core.ratelimit import RateLimit, client_ip, enforce
from app.schemas.auth import CustomerOut, RefreshIn, RegisterIn, TokenOut
from app.services import auth as auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimit("register", settings.rate_limit_register))],
)
async def register(payload: RegisterIn, session: SessionDep) -> TokenOut:
    customer = await auth_service.register(
        session,
        email=payload.email,
        full_name=payload.full_name,
        password=payload.password,
        referral_code=payload.referral_code,
    )
    access, refresh, ttl = auth_service.issue_tokens(customer)
    return TokenOut(access_token=access, refresh_token=refresh, expires_in=ttl)


@router.post("/login", response_model=TokenOut)
async def login(
    request: Request,
    session: SessionDep,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenOut:
    # Limit per IP *and* per account, so one attacker cannot spray many
    # accounts from one host, nor many hosts at one account.
    await enforce("login_ip", client_ip(request), settings.rate_limit_login)
    await enforce("login_user", form.username.strip().lower(), settings.rate_limit_login)

    customer = await auth_service.authenticate(session, email=form.username, password=form.password)
    access, refresh, ttl = auth_service.issue_tokens(customer)
    return TokenOut(access_token=access, refresh_token=refresh, expires_in=ttl)


@router.post("/refresh", response_model=TokenOut)
async def refresh(payload: RefreshIn, session: SessionDep) -> TokenOut:
    _, access, new_refresh, ttl = await auth_service.rotate_refresh_token(
        session, payload.refresh_token
    )
    return TokenOut(access_token=access, refresh_token=new_refresh, expires_in=ttl)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshIn) -> None:
    await auth_service.logout(payload.refresh_token)


@router.get("/me", response_model=CustomerOut)
async def me(customer: CurrentCustomer) -> CustomerOut:
    return CustomerOut.model_validate(customer)
