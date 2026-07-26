"""Declarative base shared by every ORM model.

IMPORTANT: Django owns the schema (see QulaySIM-admin). These models MAP onto
existing tables — there is no Alembic here on purpose. `tests/integration/
test_schema_contract.py` fails the build if the two drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Django declares `created_at` as NOT NULL with *no database default*
    (auto_now_add is ORM-level). Every insert from this service must therefore
    supply the timestamp itself."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass
