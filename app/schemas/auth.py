from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field, field_validator, model_validator

from app.domain.passwords import MIN_LENGTH as MIN_PASSWORD_LENGTH
from app.schemas.base import APIModel


class RegisterIn(APIModel):
    email: EmailStr
    full_name: str = Field(default="", max_length=150)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    referral_code: str | None = Field(default=None, max_length=12)

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _password_is_strong(self) -> RegisterIn:
        from pydantic_core import PydanticCustomError

        from app.domain.passwords import rejection

        result = rejection(self.password, email=str(self.email), full_name=self.full_name)
        if result:
            # PydanticCustomError rather than ValueError so the rule's code lands
            # in the error's `type`. A plain ValueError carries only English
            # prose, which leaves the storefront choosing between showing that
            # untranslated or saying nothing useful — and saying nothing useful
            # is what made this form look broken to anyone who typed a weak
            # password.
            raise PydanticCustomError(
                result.code,
                result.message,
                {"limit": result.limit} if result.limit is not None else {},
            )
        return self


class TokenOut(APIModel):
    """Only the access token is returned.

    The refresh token is delivered as an httpOnly cookie so that no script —
    including an injected one — can read the long-lived credential.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshIn(APIModel):
    """Fallback for clients that cannot hold cookies; browsers use the
    httpOnly `qs_refresh` cookie instead."""

    refresh_token: str | None = None


class CustomerOut(APIModel):
    id: int
    email: str
    full_name: str
    created_at: datetime
    # Whether this customer has ever paid for an order. Drives hiding the
    # first-order promo strip; counted from paid orders rather than from eSIMs,
    # because an order that was paid and never fulfilled has still spent the
    # welcome discount.
    has_purchases: bool = False


class ProfileUpdateIn(APIModel):
    full_name: str | None = Field(default=None, max_length=150)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=128)

class GoogleIn(APIModel):
    """The credential Google Identity Services hands the browser."""

    # Bounded so a huge body is refused by validation before any crypto runs.
    credential: str = Field(min_length=32, max_length=8192)


class ProvidersOut(APIModel):
    """Which social buttons to render, and with what public client id."""

    google_client_id: str = ""


# The complete set of reasons the storefront may report when Google sign-in
# dies in the browser. A fixed allow-list rather than free text: the value ends
# up in a log line written by an unauthenticated caller, and a caller must
# never be able to choose what that line says. Anything not listed here is a
# 422 and never reaches the log.
GoogleFailureReason = Literal[
    # The GSI script itself never loaded — CSP, an extension, or the network.
    "script_blocked",
    # The script loaded but no usable button ever appeared. This is what a
    # rejected origin looks like from the page: GSI logs to the console and
    # calls nothing back.
    "button_not_rendered",
    # GSI's own error_callback types.
    "popup_failed_to_open",
    "popup_closed",
    # The credential callback fired with nothing in it.
    "credential_missing",
    # error_callback fired with a type we do not recognise.
    "unknown",
]


class GoogleFailureIn(APIModel):
    """A report that Google sign-in failed before any credential existed."""

    reason: GoogleFailureReason
