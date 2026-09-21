"""Sign in, refresh, sign out, and who am I."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import SessionDep
from app.api.v1.routers.backoffice.deps import CurrentStaff
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.ratelimit import RateLimit, client_ip
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    is_revoked,
    revoke,
)
from app.db.models import Staff
from app.services.backoffice import auth as service

router = APIRouter(prefix="/api/v1/backoffice/auth", tags=["backoffice"])

# The credential. httpOnly, so no script on the page can read it, and scoped to
# the one path that consumes it.
REFRESH_COOKIE = "qs_bo_refresh"
REFRESH_PATH = "/api/v1/backoffice/auth"

# A companion the page IS allowed to read. It holds nothing — its presence is
# the only bit of information — so the app can skip a refresh call that would
# only 401 for someone who never signed in.
HINT_COOKIE = "qs_bo"

# limit/seconds, matching every other bucket in this service. Ten tries a
# minute per IP is generous for a human and useless for a script; the per-name
# lockout in the service is the sharper of the two brakes.
_login_limit = RateLimit("backoffice_login", "10/60")


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, max_length=8)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - a scheme name, not a secret


class StaffOut(BaseModel):
    id: int
    name: str
    email: str
    role: str


def _shape(staff: Staff) -> StaffOut:
    return StaffOut(
        id=staff.id, name=staff.display_name, email=staff.email or staff.username, role=staff.role
    )


def _issue(response: Response, staff: Staff) -> TokenOut:
    subject = service.subject(staff)
    refresh, _ = create_refresh_token(subject, service.REFRESH_TTL)
    secure = settings.is_production
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=int(service.REFRESH_TTL.total_seconds()),
        path=REFRESH_PATH,
        httponly=True,
        secure=secure,
        samesite="strict",
    )
    response.set_cookie(
        HINT_COOKIE,
        "1",
        max_age=int(service.REFRESH_TTL.total_seconds()),
        path="/",
        httponly=False,
        secure=secure,
        samesite="strict",
    )
    return TokenOut(access_token=create_access_token(subject))


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn, request: Request, response: Response, session: SessionDep
) -> TokenOut:
    await _login_limit(request)
    staff = await service.sign_in(
        session,
        login=payload.email,
        password=payload.password,
        code=payload.code,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    return _issue(response, staff)


@router.post("/refresh", response_model=TokenOut)
async def refresh(request: Request, response: Response, session: SessionDep) -> TokenOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthenticationError("Sessiya yo‘q")
    payload = decode_token(token, "refresh")
    if await is_revoked(str(payload["jti"])):
        raise AuthenticationError("Sessiya bekor qilingan")
    staff = await service.load(session, service.staff_id_from(str(payload["sub"])))
    if staff is None:
        raise AuthenticationError("Hisob topilmadi yoki o‘chirilgan")
    # Rotate: the refresh token just used is burned, so a stolen copy is worth
    # one use at most and the theft shows up as the real person being signed out.
    await revoke(str(payload["jti"]), int(service.REFRESH_TTL.total_seconds()))
    return _issue(response, staff)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, "refresh")
            await revoke(str(payload["jti"]), int(service.REFRESH_TTL.total_seconds()))
        except AuthenticationError:
            pass  # Already invalid; clearing the cookies is still the right answer.
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, httponly=True, samesite="strict")
    response.delete_cookie(HINT_COOKIE, path="/", samesite="strict")
    response.status_code = 204
    return response


@router.get("/me", response_model=StaffOut)
async def me(staff: CurrentStaff) -> StaffOut:
    return _shape(staff)
