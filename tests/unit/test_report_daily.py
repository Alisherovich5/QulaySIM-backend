"""The period, day by day.

Four totals tell an owner the week was worth six million so'm. They cannot tell
them whether that is a good week, which is the only question anybody opens a
reports page with — and the answer is in the shape of the line, not the sum.

Two properties matter more than the arithmetic. Every day in the window must
appear, including the ones with no trade: a chart that skips an empty Sunday
draws a straight line across it and calls it steady. And a day's profit must be
computed the same way the period's profit is, or the chart and the tile above
it disagree in front of the person deciding what to charge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.v1.routers.backoffice.money import Day, _daily


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    """Answers the two grouped queries in the order `_daily` makes them."""

    def __init__(self, orders: list[tuple], costs: list[tuple]) -> None:
        self._answers = [FakeResult(orders), FakeResult(costs)]

    async def execute(self, _statement: object) -> FakeResult:
        return self._answers.pop(0)


def window(days: int) -> datetime:
    return datetime(2026, 9, 22, 12, 0, tzinfo=UTC) - timedelta(days=days)


@pytest.mark.anyio
async def test_a_day_with_no_trade_is_a_zero_not_a_gap() -> None:
    session = FakeSession(orders=[("2026-09-20", 2, 100_000)], costs=[("2026-09-20", Decimal("3"))])

    days = await _daily(session, window(3), 3, Decimal("12000"))

    assert [d.date for d in days] == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert [d.orders for d in days] == [2, 0, 0]
    assert [d.revenue_uzs for d in days] == [100_000.0, 0.0, 0.0]


@pytest.mark.anyio
async def test_profit_is_revenue_less_cost_at_the_period_rate() -> None:
    # The same rate the totals use, passed in rather than fetched again: two
    # lookups are two chances for the chart and the tile to disagree.
    session = FakeSession(orders=[("2026-09-22", 1, 90_000)], costs=[("2026-09-22", Decimal("3"))])

    days = await _daily(session, window(1), 1, Decimal("12000"))

    assert days[0].profit_uzs == 90_000 - 3 * 12_000


@pytest.mark.anyio
async def test_a_day_that_cost_more_than_it_earned_reports_a_loss() -> None:
    """Not clamped to zero. This shop has sold below cost before, and a chart
    that floors at zero is one that cannot show it happening again."""
    session = FakeSession(orders=[("2026-09-22", 1, 10_000)], costs=[("2026-09-22", Decimal("5"))])

    days = await _daily(session, window(1), 1, Decimal("12000"))

    assert days[0].profit_uzs == 10_000 - 60_000
    assert days[0].profit_uzs < 0


@pytest.mark.anyio
async def test_the_window_is_as_long_as_it_was_asked_for() -> None:
    session = FakeSession(orders=[], costs=[])

    days = await _daily(session, window(90), 90, Decimal("12000"))

    assert len(days) == 90
    assert all(isinstance(d, Day) for d in days)
    assert all(d.orders == 0 and d.revenue_uzs == 0.0 for d in days)


@pytest.mark.anyio
async def test_a_cost_on_a_day_with_no_sale_still_shows_as_a_loss() -> None:
    """A complimentary grant: cost lands in the period, revenue does not. The
    day should read as money spent, because it was."""
    session = FakeSession(orders=[], costs=[("2026-09-22", Decimal("4"))])

    days = await _daily(session, window(1), 1, Decimal("12000"))

    assert days[0].revenue_uzs == 0.0
    assert days[0].profit_uzs == -48_000
