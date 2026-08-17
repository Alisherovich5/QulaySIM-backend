from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentCustomer, SessionDep
from app.core.config import settings
from app.core.cookies import (
    REFRESH_COOKIE,
    clear_refresh_cookie,
    clear_session_hint,
    set_refresh_cookie,
    set_session_hint,
)
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.ratelimit import RateLimit, client_ip, enforce
from app.db.models import Customer
from app.schemas.auth import (
    CustomerOut,
    GoogleFailureIn,
    GoogleIn,
    ProvidersOut,
    RefreshIn,
    RegisterIn,
    TokenOut,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

logger = get_logger(__name__)


def _issue(response: Response, customer: Customer) -> TokenOut:
    """Access token in the body, refresh token in an httpOnly cookie.

    The refresh token is deliberately absent from the response body: if
    JavaScript can read it, so can an XSS, and it is worth 30 days of access.
    """
    access, refresh, ttl = auth_service.issue_tokens(customer)
    set_refresh_cookie(response, refresh)
    set_session_hint(response)
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


@router.post(
    "/refresh",
    response_model=TokenOut,
    # Unauthenticated and cheap to call, so it needs the same brute-force
    # ceiling as login — otherwise it is a free oracle for guessing tokens.
    dependencies=[Depends(RateLimit("refresh", settings.rate_limit_login))],
)
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
    set_session_hint(response)
    return TokenOut(access_token=access, expires_in=ttl)


@router.post(
    "/google",
    response_model=TokenOut,
    # Same ceiling as login: unauthenticated, and each call costs a signature
    # verification, so it must not be a free CPU sink either.
    dependencies=[Depends(RateLimit("google", settings.rate_limit_login))],
)
async def google(payload: GoogleIn, session: SessionDep, response: Response) -> TokenOut:
    """Exchange a Google ID token for our own session.

    Returns 401 for any verification failure. The reason is logged, not
    returned: the caller does not need to learn which check it failed, and the
    front end only ever offers "try again or use a password".
    """
    customer = await auth_service.login_with_google(session, credential=payload.credential)
    return _issue(response, customer)


@router.post(
    "/google/failed",
    status_code=status.HTTP_204_NO_CONTENT,
    # The same ceiling as the sign-in it reports on: a diagnostic must not be
    # cheaper to call than the thing it diagnoses.
    dependencies=[Depends(RateLimit("google_failed", settings.rate_limit_login))],
)
async def google_failed(payload: GoogleFailureIn) -> None:
    """Record that the Google button never produced a credential.

    When Google Identity Services refuses — a rejected origin, a blocked
    script, a popup that never opens — it writes to the browser console and
    calls nothing back, so /google is never reached and the API sees a silent
    nothing. The only report the operator gets today is a customer saying the
    button does not work. This exists purely to put one line in the log.

    Nothing is stored and nothing is returned. The reason comes from a fixed
    allow-list (GoogleFailureReason), so no caller-chosen text can reach the
    log, and no identifier of the person is recorded: the line answers "which
    failure", never "who". That means it is evidence that a failure happened
    and of what kind — not proof of who hit it, which is all it needs to be.
    """
    logger.warning("auth.google_client_failed", reason=payload.reason)


@router.get("/providers", response_model=ProvidersOut)
async def providers() -> ProvidersOut:
    """Which sign-in buttons the storefront should render.

    The client id lives here rather than in the front-end build so that turning
    Google on is an environment change on the server, not a rebuild — and so a
    deployment without it shows no button instead of a broken one.
    """
    from app.integrations import google_auth

    from app.domain.passwords import MIN_LENGTH

    return ProvidersOut(
        google_client_id=settings.google_client_id if google_auth.is_configured() else "",
        min_password_length=MIN_LENGTH,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    payload: RefreshIn | None = None,
    qs_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    await auth_service.logout(qs_refresh or (payload.refresh_token if payload else None))
    clear_refresh_cookie(response)
    clear_session_hint(response)


@router.get("/me", response_model=CustomerOut)
async def me(customer: CurrentCustomer, session: SessionDep) -> CustomerOut:
    """The signed-in customer, plus whether they have bought before.

    One extra count per call, on an endpoint the storefront hits once per page
    load. It buys the difference between advertising the welcome discount to
    everybody and advertising it only to people it still applies to.
    """
    from app.repositories import orders as order_repo

    out = CustomerOut.model_validate(customer)
    paid = await order_repo.count_paid_orders(session, customer.id)
    return out.model_copy(update={"has_purchases": paid > 0})
