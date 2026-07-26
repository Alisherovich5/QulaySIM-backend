from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.schemas.base import APIModel

MIN_PASSWORD_LENGTH = 8


class RegisterIn(APIModel):
    email: EmailStr
    full_name: str = Field(default="", max_length=150)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    referral_code: str | None = Field(default=None, max_length=12)

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


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


class ProfileUpdateIn(APIModel):
    full_name: str | None = Field(default=None, max_length=150)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=128)
