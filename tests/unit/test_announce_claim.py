"""The claim that guards the sale announcement, and the retry it nearly killed.

Order #129 was delivered in full — two eSIMs, both synced — and never appeared
in the operations chat. The send failed at 05:42:55 with an asyncio error, the
task retried at 05:44:55 exactly as designed, and the retry found the claim its
own first attempt had taken and returned "duplicate" without sending anything.

The claim has to be taken before the send: two workers racing on the same order
must not both post. So the fix is to give it back when the send fails, and
these tests are here because the failure was silent — a green retry that
delivered nothing looks identical to a success in the logs.
"""

from __future__ import annotations

import pytest

from app.workers.tasks import reports

pytestmark = pytest.mark.anyio


class _FakeRedis:
    """Enough of redis for SETNX and DELETE, and it records what happened."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.deletes = 0

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key):
        self.deletes += 1
        return self.keys.pop(key, None) is not None


@pytest.fixture
def fake_redis(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(reports.redis, "from_url", lambda _url: client)
    return client


class TestTheClaim:
    def test_the_first_caller_takes_it(self, fake_redis) -> None:
        assert reports._claim_announcement(129) is True

    def test_the_second_caller_is_turned_away(self, fake_redis) -> None:
        reports._claim_announcement(129)

        assert reports._claim_announcement(129) is False

    def test_releasing_it_lets_the_retry_take_it_again(self, fake_redis) -> None:
        """The whole bug, in three lines."""
        assert reports._claim_announcement(129) is True
        reports._release_announcement(129)

        assert reports._claim_announcement(129) is True

    def test_releasing_touches_only_that_order(self, fake_redis) -> None:
        reports._claim_announcement(128)
        reports._claim_announcement(129)

        reports._release_announcement(129)

        assert reports._claim_announcement(128) is False
        assert reports._claim_announcement(129) is True

    def test_a_redis_outage_does_not_raise_out_of_the_release(self, monkeypatch) -> None:
        """Losing the release is one sale announced twice; raising here would
        replace a duplicate message with a lost retry."""

        def _boom(_url):
            raise OSError("redis is down")

        monkeypatch.setattr(reports.redis, "from_url", _boom)

        reports._release_announcement(129)  # no exception

    def test_a_redis_outage_still_lets_the_sale_be_announced(self, monkeypatch) -> None:
        """Announcing twice is a nuisance; staying silent about a sale is worse."""

        def _boom(_url):
            raise OSError("redis is down")

        monkeypatch.setattr(reports.redis, "from_url", _boom)

        assert reports._claim_announcement(129) is True


class TestTheTaskGivesTheClaimBack:
    """The helpers above are correct in isolation; #129 failed in the wiring.

    So this drives `announce_sale` itself: make the send fail the way Telegram
    failed that morning, and check the next attempt is able to send rather than
    reporting a duplicate. Without the release in the except branch this test
    fails and the four above still pass.
    """

    @pytest.fixture
    def wired(self, monkeypatch, fake_redis):
        from contextlib import contextmanager

        @contextmanager
        def _session():
            yield object()

        monkeypatch.setattr(reports, "worker_session", _session)
        monkeypatch.setattr(reports, "build_sale_note", lambda _s, _o: "🎉 Yangi sotuv")
        return fake_redis

    def test_a_failed_send_leaves_the_claim_free(self, wired, monkeypatch) -> None:
        def _fails(_text):
            raise OSError("Event loop is closed")

        monkeypatch.setattr(reports, "send_html_blocking", _fails)

        with pytest.raises(Exception):  # noqa: B017 - Celery raises Retry here
            reports.announce_sale.apply(args=(129,), throw=True)

        assert wired.deletes == 1
        # The retry can now do what the retry is for.
        assert reports._claim_announcement(129) is True

    def test_a_delivered_sale_keeps_the_claim(self, wired, monkeypatch) -> None:
        sent: list[str] = []
        monkeypatch.setattr(reports, "send_html_blocking", sent.append)

        assert reports.announce_sale.apply(args=(129,), throw=True).get() == "sent"
        assert sent == ["🎉 Yangi sotuv"]
        # Held, so a Celery redelivery of the same task cannot post it twice.
        assert wired.deletes == 0
        assert reports._claim_announcement(129) is False


class TestTheWorkerOwnsItsEventLoop:
    """Why `send_html_blocking` exists at all.

    `asyncio.run()` opens a new loop each call, and the two things `send_html`
    reaches for by default are process-global: the SQLAlchemy engine behind the
    recipient list and the shared httpx client. Bound to the first loop, they
    fail on the second call — "got Future attached to a different loop", then
    "Event loop is closed". The worker path must therefore reach for neither.
    """

    def test_the_shared_http_client_is_never_touched(self, monkeypatch) -> None:
        from app.integrations import http as http_module
        from app.integrations import telegram

        def _forbidden():
            raise AssertionError("the worker path must not use the shared client")

        monkeypatch.setattr(http_module, "get_client", _forbidden)
        monkeypatch.setattr(telegram, "get_client", _forbidden)
        monkeypatch.setattr(telegram.settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(telegram.settings, "telegram_chat_id", "-100123")

        posted: list[str] = []

        class _Response:
            @staticmethod
            def raise_for_status():
                return None

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, _url, **kwargs):
                posted.append(kwargs["json"]["chat_id"])
                return _Response()

        monkeypatch.setattr(telegram.httpx, "AsyncClient", lambda **_kw: _Client())

        # And not the app's pooled engine either — that is the half of the
        # bug that produced "got Future attached to a different loop". The spy
        # is what pins it: swapping task_sessions() back for session_scope()
        # leaves `reached` empty.
        reached: list[str] = []

        # A plain function, not a coroutine: `async with` on a coroutine object
        # raises before the body ever runs, so a coroutine spy records nothing.
        def _no_db():
            reached.append("task_sessions")
            raise OSError("no database in this test")

        monkeypatch.setattr("app.db.session.task_sessions", _no_db)

        telegram.send_html_blocking("hello")

        assert reached == ["task_sessions"]
        assert posted == ["-100123"]

    def test_it_can_be_called_twice_in_one_process(self, monkeypatch) -> None:
        """The actual failure: the first call worked and the second did not."""
        from app.integrations import telegram

        monkeypatch.setattr(telegram.settings, "telegram_bot_token", "test-token")
        monkeypatch.setattr(telegram.settings, "telegram_chat_id", "-100123")

        calls: list[str] = []

        class _Response:
            @staticmethod
            def raise_for_status():
                return None

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, _url, **kwargs):
                calls.append(kwargs["json"]["text"])
                return _Response()

        monkeypatch.setattr(telegram.httpx, "AsyncClient", lambda **_kw: _Client())

        def _no_db():
            raise OSError("no database in this test")

        monkeypatch.setattr("app.db.session.task_sessions", _no_db)

        telegram.send_html_blocking("birinchi")
        telegram.send_html_blocking("ikkinchi")

        assert calls == ["birinchi", "ikkinchi"]
