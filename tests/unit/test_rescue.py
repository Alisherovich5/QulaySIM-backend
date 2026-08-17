"""The one guarantee this shop cannot compromise on.

"Paid, but no eSIM" is the only failure here that takes a customer's money and
gives nothing back, and it happens abroad, where they cannot walk into a shop.
Everything else in the system is allowed to degrade; this is not.

The retries and the late acknowledgement cover a task that ran and failed. These
tests cover the gap those cannot see: an order that is paid while no task exists
to fulfil it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.workers.tasks import rescue


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Just enough session to hand the task a row set."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, _statement):
        return _Result(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    """Record what would have been dispatched and alerted, without doing it."""
    state: dict[str, list] = {"dispatched": [], "alerts": []}

    class _Task:
        @staticmethod
        def delay(order_id: int) -> None:
            state["dispatched"].append(order_id)

    import app.workers.tasks.provisioning as provisioning

    monkeypatch.setattr(provisioning, "fulfil_paid_order", _Task)
    monkeypatch.setattr(
        rescue, "_alert", lambda ids, overflow: state["alerts"].append((ids, overflow))
    )
    return state


def _rows(*ages: timedelta):
    now = datetime.now(UTC)
    return [(index + 1, now - age, now - age) for index, age in enumerate(ages)]


def _run(monkeypatch, rows):
    monkeypatch.setattr(rescue, "worker_session", lambda: _Session(rows))
    return rescue.rescue_unfulfilled_orders()


class TestWhatItRescues:
    def test_a_fresh_order_is_left_alone(self, monkeypatch, captured) -> None:
        """Fulfilment takes seconds; re-dispatching sooner fights the task's own
        backoff instead of helping."""
        result = _run(monkeypatch, _rows(timedelta(seconds=30)))
        assert result["redispatched"] == 0
        assert captured["dispatched"] == []

    def test_an_order_stuck_past_five_minutes_is_dispatched_again(
        self, monkeypatch, captured
    ) -> None:
        result = _run(monkeypatch, _rows(timedelta(minutes=6)))
        assert result["redispatched"] == 1
        assert captured["dispatched"] == [1]

    def test_it_does_not_alert_for_a_recent_hiccup(self, monkeypatch, captured) -> None:
        """A six-minute delay that the retry fixes is not worth a phone buzzing."""
        _run(monkeypatch, _rows(timedelta(minutes=6)))
        assert captured["alerts"] == []

    def test_past_twenty_minutes_somebody_is_told(self, monkeypatch, captured) -> None:
        """By then a customer is abroad with a receipt and no internet."""
        result = _run(monkeypatch, _rows(timedelta(minutes=25)))
        assert result["alarming"] == 1
        assert captured["alerts"] and captured["alerts"][0][0] == [1]

    def test_it_still_dispatches_when_it_alerts(self, monkeypatch, captured) -> None:
        """The alert tells a human; the dispatch is what fixes it."""
        _run(monkeypatch, _rows(timedelta(minutes=25)))
        assert captured["dispatched"] == [1]


class TestItCannotMakeThingsWorse:
    def test_an_outage_cannot_become_a_thousand_dispatches(self, monkeypatch, captured) -> None:
        rows = _rows(*[timedelta(minutes=30)] * (rescue.MAX_PER_RUN + 5))
        result = _run(monkeypatch, rows)
        assert result["redispatched"] == rescue.MAX_PER_RUN

    def test_what_was_skipped_is_reported_rather_than_hidden(self, monkeypatch, captured) -> None:
        """A silent cap reads as "all clear" when it is the opposite."""
        rows = _rows(*[timedelta(minutes=30)] * (rescue.MAX_PER_RUN + 5))
        result = _run(monkeypatch, rows)
        assert result["skipped"] > 0
        assert captured["alerts"][0][1] == result["skipped"]

    def test_a_row_with_no_timestamp_is_skipped_not_crashed(self, monkeypatch, captured) -> None:
        """Rows predating the paid_at column must not take the sweep down with
        them — the sweep is what protects every other order in the list."""
        result = _run(monkeypatch, [(1, None, None)])
        assert result["redispatched"] == 0

    def test_a_naive_timestamp_is_treated_as_utc(self, monkeypatch, captured) -> None:
        """Postgres can hand back a naive datetime; comparing it to an aware one
        raises, and a sweep that raises protects nobody."""
        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
        result = _run(monkeypatch, [(7, naive, naive)])
        assert result["redispatched"] == 1
