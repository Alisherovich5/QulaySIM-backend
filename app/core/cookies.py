"""Refresh-token cookie handling.

The refresh token is the long-lived credential (30 days), so it must never be
reachable from JavaScript: an XSS that can read `localStorage` would otherwise
walk away with a month of access. It lives in an httpOnly cookie instead.

The access token stays in the response body. It is short-lived (30 minutes) and
the client keeps it in memory only, so a page reload silently re-mints it from
the cookie rather than persisting anything a script could read.

SameSite is Lax. It used to be None, justified by the storefront and the API
living on different hosts — that stopped being true: the front end calls a
relative `/api` on qulaysim.uz, the only allowed CORS origin is qulaysim.uz
itself, and the admin never touches this API. None meant the browser attached
the refresh cookie to cross-site requests it never needed to, which is a CSRF
surface kept open for a reason that had expired. Lax still covers every real
flow, including the Google sign-in popup: the credential comes back through a
same-origin XHR.
"""

from __future__ import annotations

from fastapi import Response

from app.core.config import settings

REFRESH_COOKIE = "qs_refresh"
REFRESH_COOKIE_PATH = "/api/auth"


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )


# A deliberately readable companion cookie. It holds no credential — only the
# fact that a refresh cookie exists — so the storefront can skip the refresh
# call for anonymous visitors instead of starting every page load with a 401.
SESSION_HINT_COOKIE = "qs_session"


def set_session_hint(response: Response) -> None:
    response.set_cookie(
        key=SESSION_HINT_COOKIE,
        value="1",
        max_age=settings.refresh_token_ttl_days * 86400,
        path="/",
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
    )


def clear_session_hint(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_HINT_COOKIE,
        path="/",
        secure=settings.is_production,
        samesite="lax",
    )
