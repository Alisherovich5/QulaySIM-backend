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


class _FakeRedis:
    """A Redis that remembers keys, or refuses to work at all."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.keys: dict[str, int | None] = {}

    def set(self, key: str, value: bytes, ex: int | None = None, nx: bool = False):
        if self.broken:
            raise RuntimeError("redis is down")
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True


@pytest.fixture
def alerting(monkeypatch):
    """Let `_alert` run for real, but against a fake Redis and no Telegram."""
    import redis

    import app.integrations.telegram as telegram

    fake = _FakeRedis()
    sent: list[str] = []
    monkeypatch.setattr(redis.Redis, "from_url", lambda url: fake)
    monkeypatch.setattr(telegram, "send_html_blocking", lambda text: sent.append(text))
    monkeypatch.setattr(rescue, "_failure_note", lambda ids: {})
    return {"redis": fake, "sent": sent}


class TestItDoesNotRepeatItself:
    """The rescue runs every five minutes. An order that stays stuck must not
    send the same message twelve times an hour — that is how an alert stops
    being read, and the one alert worth reading is this one."""

    def test_the_first_time_an_order_is_stuck_it_is_announced(self, alerting) -> None:
        rescue._alert([141], 0)
        assert len(alerting["sent"]) == 1
        assert "#141" in alerting["sent"][0]

    def test_the_second_run_says_nothing(self, alerting) -> None:
        rescue._alert([141], 0)
        rescue._alert([141], 0)
        assert len(alerting["sent"]) == 1

    def test_a_different_order_still_gets_through(self, alerting) -> None:
        """Silence is per order, not a mute on the whole alert."""
        rescue._alert([141], 0)
        rescue._alert([142], 0)
        assert len(alerting["sent"]) == 2
        assert "#142" in alerting["sent"][1]

    def test_the_silence_expires(self, alerting) -> None:
        """A day, not forever: somebody has paid and has nothing, so an order
        nobody fixes should keep asking once a day rather than stop asking."""
        rescue._alert([141], 0)
        assert alerting["redis"].keys["rescue:alerted:141"] == rescue.ALERT_SILENCE
        assert rescue.ALERT_SILENCE >= 60 * 60

    def test_nothing_fresh_means_no_message_at_all(self, alerting) -> None:
        """Not an empty bulletin — no bulletin."""
        rescue._alert([141], 0)
        alerting["sent"].clear()
        rescue._alert([141], 0)
        assert alerting["sent"] == []

    def test_a_broken_redis_alerts_rather_than_swallows(self, monkeypatch, alerting) -> None:
        """A duplicate message is a smaller failure than a silent one."""
        import redis

        monkeypatch.setattr(redis.Redis, "from_url", lambda url: _FakeRedis(broken=True))
        rescue._alert([141], 0)
        rescue._alert([141], 0)
        assert len(alerting["sent"]) == 2


class TestItSaysWhyItFailed:
    """ "Check the supplier" is the one thing the reader cannot do from their
    phone. The supplier already told us; the message should repeat it."""

    def test_the_supplier_reason_is_in_the_message(self, monkeypatch, alerting) -> None:
        monkeypatch.setattr(
            rescue, "_failure_note", lambda ids: {141: "Insufficient Wallet Balance"}
        )
        rescue._alert([141], 0)
        assert "Insufficient Wallet Balance" in alerting["sent"][0]

    def test_an_order_with_no_reason_is_still_announced(self, alerting) -> None:
        rescue._alert([141], 0)
        assert "#141" in alerting["sent"][0]

    def test_only_the_latest_attempt_is_quoted(self, monkeypatch) -> None:
        """One line per order: the newest row wins, older failures do not
        stack up into a wall of text nobody reads."""
        rows = [(141, "newest reason"), (141, "older reason")]
        monkeypatch.setattr(rescue, "worker_session", lambda: _Session(rows))
        assert rescue._failure_note([141]) == {141: "newest reason"}

    def test_no_orders_means_no_query(self, monkeypatch) -> None:
        def _explode():
            raise AssertionError("the database must not be touched for an empty list")

        monkeypatch.setattr(rescue, "worker_session", _explode)
        assert rescue._failure_note([]) == {}
