"""Domain exceptions and RFC 7807-style error responses.

Routers raise domain errors; a single handler turns them into HTTP responses.
Business rules therefore never import `fastapi.HTTPException`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)


class DomainError(Exception):
    """Base class for expected, client-facing failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "domain_error"

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code
        self.extra = extra or {}


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(DomainError):
    """Input the client can fix. Carries a code so the reason can be translated."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class AuthenticationError(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class PermissionDeniedError(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitedError(DomainError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"

    def __init__(self, detail: str, retry_after: int):
        super().__init__(detail)
        self.retry_after = retry_after


class UpstreamError(DomainError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


class ServiceUnavailableError(DomainError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def _body(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    # `detail` is kept at the top level because the storefront reads
    # `error.response.data.detail` — changing it would break the UI contract.
    return {"code": code, "detail": detail, "request_id": request_id_var.get(), **extra}


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Strip anything the JSON encoder cannot handle.

    A validator that raises ValueError puts the exception *object* into the
    error's `ctx`. Serialising that fails, which turned every rejected
    password into a 500 instead of the 422 the client needs to show a message.
    """
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {
            "type": error.get("type"),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
        }
        ctx = error.get("ctx")
        if ctx:
            entry["ctx"] = {key: str(value) for key, value in ctx.items()}
        cleaned.append(entry)
    return cleaned


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_r: Request, exc: DomainError) -> ORJSONResponse:
        headers = {}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after)
        if isinstance(exc, AuthenticationError):
            headers["WWW-Authenticate"] = "Bearer"
        return ORJSONResponse(
            _body(exc.code, exc.detail, **exc.extra),
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError) -> ORJSONResponse:
        return ORJSONResponse(
            _body(
                "validation_error",
                "Request payload is invalid",
                errors=_serialisable_errors(exc),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_r: Request, exc: IntegrityError) -> ORJSONResponse:
        logger.warning("db.integrity_error", error=str(exc.orig))
        return ORJSONResponse(
            _body("conflict", "That record already exists"),
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_r: Request, exc: Exception) -> ORJSONResponse:
        logger.exception("unhandled_error", error=str(exc))
        # Never leak internals to the client.
        return ORJSONResponse(
            _body("internal_error", "Something went wrong on our side"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
