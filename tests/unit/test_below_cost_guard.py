"""Never sell a plan for less than the wholesaler charges for it.

The shop owner asked what happens when a supplier's price rises past ours: does
our price follow, or do we keep selling at the old one? For the 1 333 plans
priced by rule it follows, because the sync recomputes them. For the 23 with a
locked price nothing moves it — so the shop would have gone on selling those and
paying the difference on every order, invisibly, because a loss looks exactly
like a sale until someone reconciles the month.

Refusing is the conservative half. The plan stops selling instead of silently
repricing under a customer already at checkout, and the report names it so it
gets fixed rather than sitting there unsold.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.checkout import sells_at_a_loss


@dataclass
class FakePlan:
    price_usd: Decimal
    cost_usd: Decimal | None


@pytest.mark.parametrize(
    ("price", "cost", "expected"),
    [
        # Healthy margin.
        ("2.00", "1.00", False),
        # A cent of margin is still margin. This rule is about losses, not about
        # whether the margin is good — that is the pricing ladder's job.
        ("1.01", "1.00", False),
        # Exactly break-even counts as a loss: the sale carries payment fees and
        # occupies a wallet balance, so zero margin is worse than no sale.
        ("1.00", "1.00", True),
        # The case the owner described: cost went from 1.00 to 3.00.
        ("2.00", "3.00", True),
    ],
)
def test_the_comparison(price: str, cost: str, expected: bool) -> None:
    assert sells_at_a_loss(FakePlan(Decimal(price), Decimal(cost))) is expected


def test_an_unknown_cost_is_not_treated_as_free() -> None:
    """Zero is what an unsynced row holds, not what a plan costs.

    Reading it as free would mark the entire catalogue profitable and disable the
    guard precisely on the rows nobody has checked.
    """
    assert sells_at_a_loss(FakePlan(Decimal("2.00"), None)) is False
    assert sells_at_a_loss(FakePlan(Decimal("2.00"), Decimal("0"))) is False
