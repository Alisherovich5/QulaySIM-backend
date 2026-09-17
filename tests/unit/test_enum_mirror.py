"""The mirrored enums must be able to name what production actually holds.

These two drifted once, and silently: `PlanScope` and `Provider` were the only
members of `app.db.models.enums` that nothing imported, so when Django grew
`topup` and `esimcard` nothing failed. The database then held rows neither enum
could name — free while they stay unused, and a rejected order the day one is
wired into a Pydantic schema.

The values below are not invented for the test: each is a string the live
database contains today, and each is a member of the Django TextChoices this
service mirrors (`catalog.models.Plan.Scope`, `catalog.models.Provider`).
"""

from __future__ import annotations

import pytest

from app.db.models.enums import (
    DiscountType,
    ESIMStatus,
    OrderStatus,
    PaymentStatus,
    PlanScope,
    Provider,
)


@pytest.mark.parametrize("value", ["local", "regional", "global", "topup"])
def test_plan_scope_names_every_scope_django_defines(value: str) -> None:
    assert PlanScope(value).value == value


@pytest.mark.parametrize("value", ["mock", "esimaccess", "esimcard"])
def test_provider_names_every_wholesaler_django_defines(value: str) -> None:
    assert Provider(value).value == value


def test_topup_is_a_scope_fulfilment_recognises() -> None:
    """Fulfilment branches on this exact string; see workers/tasks/provisioning."""
    assert PlanScope.TOPUP == "topup"


def test_esimcard_is_a_provider_routing_recognises() -> None:
    """`suppliers._SUPPLIERS` is keyed by this exact string."""
    from app.integrations.suppliers import get_supplier

    assert Provider.ESIMCARD == "esimcard"
    assert get_supplier(Provider.ESIMCARD) is not None


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (OrderStatus, ["pending", "paid", "cancelled", "refunded"]),
        (ESIMStatus, ["pending", "active", "expired"]),
        (PaymentStatus, ["success", "failed", "refunded"]),
        (DiscountType, ["percent", "fixed"]),
    ],
)
def test_the_enums_already_in_use_still_match(enum: type, values: list[str]) -> None:
    assert sorted(member.value for member in enum) == sorted(values)
