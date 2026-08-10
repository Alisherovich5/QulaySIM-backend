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
def test_the_env_fallback_is_parsed(raw: str, expected: list[str]) -> None:
    settings.telegram_chat_id = raw
    assert telegram.env_chat_ids() == expected


def _fixed(ids: list[str]):
    """Stand in for the database lookup with a known list."""

    async def _ids() -> list[str]:
        return ids

    return _ids


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
    # Patched rather than seeded: these tests are about the fan-out, and the
    # database lookup has its own test below.
    monkeypatch.setattr(telegram, "chat_ids", _fixed(["111", "222", "333"]))
    client = _Client()
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    await telegram.send_html("<b>hi</b>")

    assert client.sent == ["111", "222", "333"]


async def test_one_blocked_recipient_does_not_stop_the_others(monkeypatch) -> None:
    """The whole reason for the loop rather than a single call."""
    monkeypatch.setattr(telegram, "chat_ids", _fixed(["111", "222"]))
    client = _Client(failing={"111"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    # No exception: 222 received it.
    await telegram.send_html("<b>hi</b>")

    assert client.sent == ["111", "222"]


async def test_total_failure_still_raises(monkeypatch) -> None:
    """Otherwise the worker would count an undelivered report as sent."""
    monkeypatch.setattr(telegram, "chat_ids", _fixed(["111", "222"]))
    client = _Client(failing={"111", "222"})
    monkeypatch.setattr(telegram, "get_client", lambda: client)

    with pytest.raises(UpstreamError):
        await telegram.send_html("<b>hi</b>")


async def test_no_recipients_is_a_configuration_error(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "chat_ids", _fixed([]))
    settings.telegram_chat_id = ""
    with pytest.raises(ServiceUnavailableError):
        await telegram.send_html("<b>hi</b>")


async def test_support_requests_reach_the_whole_list(monkeypatch) -> None:
    """Support used to POST once with the raw string, so a list broke it."""
    monkeypatch.setattr(telegram, "chat_ids", _fixed(["111", "222"]))
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


async def test_the_admin_list_wins_over_the_environment(monkeypatch) -> None:
    """Adding a colleague must not require an SSH session and an .env edit.

    Seeds two recipients, one switched off, and asserts only the live one is
    used — and that the environment value is ignored while the table has rows.
    """
    import uuid

    from app.db.models import TelegramRecipient
    from app.db.session import session_scope

    settings.telegram_chat_id = "999-from-env"
    live = f"1{uuid.uuid4().int % 10**9}"
    muted = f"2{uuid.uuid4().int % 10**9}"

    async with session_scope() as session:
        session.add(TelegramRecipient(chat_id=live, label="live", is_active=True))
        session.add(TelegramRecipient(chat_id=muted, label="muted", is_active=False))
        await session.commit()

    try:
        ids = await telegram.chat_ids()
        assert live in ids
        assert muted not in ids
        assert "999-from-env" not in ids
    finally:
        from sqlalchemy import delete

        async with session_scope() as session:
            await session.execute(
                delete(TelegramRecipient).where(TelegramRecipient.chat_id.in_([live, muted]))
            )
            await session.commit()


async def test_an_empty_table_falls_back_to_the_environment() -> None:
    """So the bot can speak before anyone has opened the admin."""
    from sqlalchemy import delete

    from app.db.models import TelegramRecipient
    from app.db.session import session_scope

    async with session_scope() as session:
        await session.execute(delete(TelegramRecipient))
        await session.commit()

    settings.telegram_chat_id = "555"
    assert await telegram.chat_ids() == ["555"]
