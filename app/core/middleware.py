"""Cross-cutting HTTP middleware: request id, access log, security headers, caching."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from app.core.config import settings
from app.core.logging import get_logger, request_id_var

logger = get_logger("http")

Handler = Callable[[Request], Awaitable[Response]]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    # This API only ever returns JSON, so nothing needs to execute or load.
    # A stray HTML error page therefore cannot run script.
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = rid
        # Announcing the server and its version only helps someone matching
        # the deployment against a CVE list.
        response.headers["Server"] = "QulaySIM"
        response.headers["Server-Timing"] = f"app;dur={duration_ms}"
        if request.url.path != "/api/health":
            logger.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # A response that mints a session must never be cached by a proxy.
        if "set-cookie" in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


# --- Caching -----------------------------------------------------------------
#
# The catalogue is the same for everybody and changes a few times a day, yet
# every visitor paid the full round trip for it: measured from Tashkent, one
# request costs ~350 ms of network against 2–4 ms of work here. Nothing in that
# ratio is fixable in the application — it is three handshakes across a
# continent — so the answer is to stop asking.
#
# `stale-while-revalidate` is the important part: after the first minute the
# browser (or a CDN in front of it) serves the copy it already has instantly and
# refreshes in the background, so nobody ever waits for the round trip.
#
# `s-maxage` is deliberately longer than `max-age`: a shared cache can hold the
# catalogue for an hour because we can purge it, while a browser we cannot reach
# gets a short leash.
_CATALOG_CACHE = "public, max-age=60, s-maxage=3600, stale-while-revalidate=86400"

#: Path prefixes whose answers are identical for every visitor.
_PUBLIC_PREFIXES = (
    "/api/regions",
    "/api/countries",
    "/api/plans",
    "/api/currency",
    "/api/content",
)


def _is_public(path: str) -> bool:
    return any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in _PUBLIC_PREFIXES
    )


class CacheHeadersMiddleware(BaseHTTPMiddleware):
    """Tell caches what may be reused, and answer unchanged requests with 304.

    Two rules, and the second one matters more than it looks: everything that is
    not explicitly public is marked `private, no-store`. An order, a profile or a
    QR code sitting in a shared cache is the kind of mistake that only shows up
    when the wrong customer sees someone else's eSIM, so the default is closed
    and the exceptions are listed by name.

    The ETag is the response body's own hash. A version counter would be cheaper
    to compute and easy to get wrong — every place that changes a price would
    have to remember to bump it — while a hash cannot drift from what it
    describes.
    """

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)

        if request.method not in ("GET", "HEAD") or not request.url.path.startswith("/api"):
            return response
        # A response that mints a session, or one that already made up its mind,
        # is left exactly as it is.
        if "set-cookie" in response.headers or "cache-control" in response.headers:
            return response

        if not _is_public(request.url.path):
            response.headers["Cache-Control"] = "private, no-store"
            return response

        if response.status_code != 200:
            return response

        # `body_iterator` lives on Starlette's streaming response, which is
        # what every route here actually returns; the base class does not
        # declare it.
        streaming = cast(StreamingResponse, response)
        chunks = [chunk async for chunk in streaming.body_iterator]
        body = b"".join(
            chunk.encode() if isinstance(chunk, str) else bytes(chunk) for chunk in chunks
        )
        headers = dict(response.headers)
        headers["Cache-Control"] = _CATALOG_CACHE
        # Catalogue answers are translated, so a cache that ignored the language
        # would serve Uzbek prices to a Russian visitor — and it would do it
        # from the edge, where we could not see it happening.
        headers["Vary"] = "Accept-Language, Accept-Encoding"
        etag = 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers["ETag"] = etag

        if request.headers.get("if-none-match") == etag:
            headers.pop("content-length", None)
            return Response(status_code=304, headers=headers)

        headers["content-length"] = str(len(body))
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


def register_middleware(app: FastAPI) -> None:
    # Added last runs first: security headers wrap the request-context logger,
    # and caching wraps both — so a 304 still carries the security headers and
    # still gets logged.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CacheHeadersMiddleware)
