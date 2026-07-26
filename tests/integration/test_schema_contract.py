"""The contract between this service and the Django schema owner.

Django (QulaySIM-admin) owns every migration; this service maps onto the tables
those migrations create. Nothing else detects drift between the two repos, so
these tests are the guard rail: if a Django migration renames or drops a column
that SQLAlchemy still maps, CI fails here rather than in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.db.base import Base
from app.db.models import ESIM, Customer, Order, Payment, Referral
from app.db.models import Testimonial as TestimonialModel
from app.db.session import engine

# Django declares these with auto_now_add — an ORM-level default, NOT a
# database default. Every INSERT from this service must supply the value
# itself or Postgres rejects the row.
MODELS_NEEDING_EXPLICIT_TIMESTAMP = [Customer, Order, ESIM, Payment, Referral, TestimonialModel]


@pytest.fixture(scope="module")
async def db_metadata() -> dict[str, dict[str, dict]]:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: {
                table: {col["name"]: col for col in inspect(sync_conn).get_columns(table)}
                for table in inspect(sync_conn).get_table_names()
            }
        )


async def test_every_mapped_table_exists(db_metadata: dict) -> None:
    missing = [t for t in Base.metadata.tables if t not in db_metadata]
    assert not missing, (
        f"Tables mapped here but absent from the database: {missing}. "
        "Run the Django migrations from QulaySIM-admin."
    )


async def test_every_mapped_column_exists(db_metadata: dict) -> None:
    problems: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        actual = db_metadata.get(table_name, {})
        for column in table.columns:
            if column.name not in actual:
                problems.append(f"{table_name}.{column.name}")
    assert not problems, f"Columns mapped here but missing in the database: {problems}"


async def test_nullability_agrees(db_metadata: dict) -> None:
    """A column this service treats as NOT NULL but Django made nullable will
    surface as a runtime AttributeError deep in a response — catch it here."""
    problems: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        actual = db_metadata.get(table_name, {})
        for column in table.columns:
            db_col = actual.get(column.name)
            if db_col is None or column.primary_key:
                continue
            if not column.nullable and db_col["nullable"]:
                problems.append(f"{table_name}.{column.name}: NOT NULL here, nullable in DB")
    assert not problems, problems


@pytest.mark.parametrize("model", MODELS_NEEDING_EXPLICIT_TIMESTAMP)
async def test_created_at_has_a_python_default(model: type) -> None:
    """Regression guard for the auto_now_add trap.

    Django's `created_at` is NOT NULL with no server default. If someone drops
    the Python-side default from a model here, every insert from this service
    starts failing with a NotNullViolation.
    """
    column = model.__table__.columns["created_at"]
    assert column.default is not None, (
        f"{model.__name__}.created_at has no Python default. Django provides no "
        "database default for auto_now_add columns, so inserts will fail."
    )


async def test_no_database_default_on_created_at() -> None:
    """Documents *why* the Python default is mandatory. If Django ever adds
    `db_default=Now()`, this test fails and the comment above can be relaxed."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'customers_customer' AND column_name = 'created_at'"
            )
        )
        row = result.first()
    assert row is not None, "customers_customer.created_at is missing"
    assert row[0] is None, (
        "Django now sets a database default for created_at — the Python-side "
        "default in app/db/base.py can be reconsidered."
    )
