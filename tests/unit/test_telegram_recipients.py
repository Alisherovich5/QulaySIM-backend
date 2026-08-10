"""Who a Telegram message reaches.

The chat id was a single string, so every report went to one person. A shop has
more than one person who needs to know that a sale happened or that a payment
failed, and the fix is a list rather than a shared account.

The case worth pinning is partial failure: one recipient blocking the bot must
not stop the report reaching everybody else, and total failure must still raise
so the worker retries instead of looking delivered.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import ServiceUnavailableError, UpstreamError
from app.integrations import telegram

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _restore_settings():
    token, chat = settings.telegram_bot_token, settings.telegram_chat_id
    settings.telegram_bot_token = "test-token"
    yield
    settings.telegram_bot_token, settings.telegram_chat_id = token, chat


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", ["123"]),
        # A group id is negative; that is how a whole staff group is reached with
        # the membership kept in Telegram rather than in .env.
        ("123,-100456", ["123", "-100456"]),
        (" 123 , -100456 ", ["123", "-100456"]),
        ("123 -100456", ["123", "-100456"]),
        ("", []),
        ("  ", []),
    ],
)
def test_the_list_is_parsed(raw: str, expected: list[str]) -> None:
    settings.telegram_chat_id = raw
    assert telegram.chat_ids() == expected


class _Response:
    def __init__(self, ok: bool):
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("telegram said no")


class _Client:
    """Records each recipient and fails the ones named in `failing`."""

    def __init__(self, failing: set[str] | None = None):
        self.sent: list[str] = []
        self.failing = failing or set()

    async def post(self, url, json, timeout):  # noqa: ANN001 - mirrors httpx
        chat = str(json["chat_id"])
        self.sent.append(chat)
        return _Response(chat not in self.failing)


async def test_every_recipient_gets_the_message(monkeypatch) -> None:
    settings.telegram_chat_id = "111,222,333"
    client = _Client()
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    await telegram.send_html("<b>hi</b>")

    assert client.sent == ["111", "222", "333"]


async def test_one_blocked_recipient_does_not_stop_the_others(monkeypatch) -> None:
    """The whole reason for the loop rather than a single call."""
    settings.telegram_chat_id = "111,222"
    client = _Client(failing={"111"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    # No exception: 222 received it.
    await telegram.send_html("<b>hi</b>")

    assert client.sent == ["111", "222"]


async def test_total_failure_still_raises(monkeypatch) -> None:
    """Otherwise the worker would count an undelivered report as sent."""
    settings.telegram_chat_id = "111,222"
    client = _Client(failing={"111", "222"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    with pytest.raises(UpstreamError):
        await telegram.send_html("<b>hi</b>")


async def test_no_recipients_is_a_configuration_error() -> None:
    settings.telegram_chat_id = ""
    with pytest.raises(ServiceUnavailableError):
        await telegram.send_html("<b>hi</b>")


async def test_support_requests_reach_the_whole_list(monkeypatch) -> None:
    """Support used to POST once with the raw string, so a list broke it."""
    settings.telegram_chat_id = "111,222"
    client = _Client()
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    await telegram.send_support_message(
        name="Ali",
        email="a@example.com",
        phone="+998900000000",
        locale="uz",
        message="salom",
        client_ip="127.0.0.1",
    )

    assert client.sent == ["111", "222"]
