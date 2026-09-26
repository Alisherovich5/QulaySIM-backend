"""Signing in with Telegram, against the real Django-owned schema.

Telegram's difference from Google is the whole subject here: it sends no
e-mail. Google's middle case — unknown provider id, known address — is what
lets a password account and a social account become one account rather than
two, and with nothing to match on that case cannot exist. So a first-time
Telegram user is asked for an address instead of being filed under a synthetic
one, and these tests pin that: the asking, the linking once it is answered, and
the fact that answering with an address that already exists joins the accounts
rather than duplicating them.

Like the Google suite, the signature verifier is stood in for — its own checks
are exhaustive in tests/unit/test_telegram_auth.py — because what is under test
here is which customer an identity resolves to.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import AuthenticationError
from app.core.security import hash_password, verify_password
from app.db.models import Customer, SocialAccount
from app.db.session import SessionFactory
from app.integrations import telegram_auth
from app.integrations.telegram_auth import TelegramAuthError, TelegramIdentity
from app.services import auth as service
from app.services.auth import TelegramEmailRequiredError

PAYLOAD = {"id": "42", "hash": "a" * 64, "auth_date": "1790000000"}


@pytest.fixture
async def session():
    async with SessionFactory() as s:
        yield s
        await s.rollback()


@pytest.fixture
def as_telegram(monkeypatch):
    """Make the verifier assert a given identity, or fail in a given way."""

    def _install(identity: TelegramIdentity | None = None, *, raises: Exception | None = None):
        def _verify(_data, *, bot_token, now=None) -> TelegramIdentity:
            if raises is not None:
                raise raises
            assert identity is not None
            return identity

        monkeypatch.setattr(telegram_auth, "verify_login", _verify)

    return _install


def _identity(uid: str | None = None) -> TelegramIdentity:
    return TelegramIdentity(
        uid=uid or f"tg-{uuid.uuid4().hex[:12]}",
        first_name="Otabek",
        last_name="Qayumov",
        username="otabek",
        photo_url="",
    )


def _email() -> str:
    return f"tg-{uuid.uuid4().hex[:12]}@example.com"


async def _links(session, customer_id: int) -> list[SocialAccount]:
    rows = await session.execute(
        select(SocialAccount).where(SocialAccount.customer_id == customer_id)
    )
    return list(rows.scalars())


class TestTheFirstTime:
    async def test_an_unknown_telegram_account_asks_for_an_address(
        self, session, as_telegram
    ) -> None:
        as_telegram(_identity())
        with pytest.raises(TelegramEmailRequiredError):
            await service.login_with_telegram(session, data=PAYLOAD)

    async def test_the_asking_is_not_an_authentication_failure(self, session, as_telegram) -> None:
        """The signature was good and we know who this is. A 401 here would
        tell somebody to fix a Telegram account that is working perfectly."""
        as_telegram(_identity())
        with pytest.raises(TelegramEmailRequiredError) as caught:
            await service.login_with_telegram(session, data=PAYLOAD)
        assert caught.value.status_code == 422
        assert caught.value.code == "telegram_email_required"

    async def test_an_address_creates_a_passwordless_account(self, session, as_telegram) -> None:
        identity = _identity()
        as_telegram(identity)
        email = _email()

        customer = await service.login_with_telegram(session, data=PAYLOAD, email=email)

        assert customer.email == email
        assert customer.full_name == "Otabek Qayumov"
        # Empty hash, and verify_password refuses an empty hash — so the account
        # cannot later be entered with a guessed password.
        assert customer.hashed_password == ""
        assert verify_password("anything", customer.hashed_password) is False

        links = await _links(session, customer.id)
        assert [(link.provider, link.provider_uid) for link in links] == [
            ("telegram", identity.uid)
        ]


class TestComingBack:
    async def test_a_known_telegram_id_needs_no_address(self, session, as_telegram) -> None:
        identity = _identity()
        as_telegram(identity)
        email = _email()
        first = await service.login_with_telegram(session, data=PAYLOAD, email=email)

        # Second visit: no e-mail supplied, and none asked for.
        again = await service.login_with_telegram(session, data=PAYLOAD)
        assert again.id == first.id

    async def test_the_id_is_what_identifies_them_not_the_address(
        self, session, as_telegram
    ) -> None:
        """An address can change hands; the provider's id cannot. Signing in
        again must land on the same customer even if a different address is
        offered alongside."""
        identity = _identity()
        as_telegram(identity)
        first = await service.login_with_telegram(session, data=PAYLOAD, email=_email())

        again = await service.login_with_telegram(session, data=PAYLOAD, email=_email())
        assert again.id == first.id
        assert len(await _links(session, first.id)) == 1


class TestJoiningAnExistingAccount:
    async def test_an_address_that_already_bought_something_is_linked_not_duplicated(
        self, session, as_telegram
    ) -> None:
        email = _email()
        existing = Customer(
            email=email, full_name="Password Signer", hashed_password=hash_password("s3cret-pass")
        )
        session.add(existing)
        await session.commit()
        await session.refresh(existing)

        as_telegram(_identity())
        joined = await service.login_with_telegram(session, data=PAYLOAD, email=email)

        assert joined.id == existing.id
        assert len(await _links(session, existing.id)) == 1

    async def test_the_password_still_works_after_linking(self, session, as_telegram) -> None:
        """Linking adds a way in; it never takes one away."""
        email = _email()
        existing = Customer(
            email=email, full_name="Password Signer", hashed_password=hash_password("s3cret-pass")
        )
        session.add(existing)
        await session.commit()

        as_telegram(_identity())
        joined = await service.login_with_telegram(session, data=PAYLOAD, email=email)
        assert verify_password("s3cret-pass", joined.hashed_password) is True

    async def test_a_disabled_account_is_refused(self, session, as_telegram) -> None:
        email = _email()
        session.add(Customer(email=email, full_name="Gone", hashed_password="", is_active=False))
        await session.commit()

        as_telegram(_identity())
        with pytest.raises(AuthenticationError):
            await service.login_with_telegram(session, data=PAYLOAD, email=email)


class TestARejectedSignature:
    async def test_is_a_401_and_says_nothing_about_which_check_failed(
        self, session, as_telegram
    ) -> None:
        as_telegram(raises=TelegramAuthError("signature mismatch"))
        with pytest.raises(AuthenticationError) as caught:
            await service.login_with_telegram(session, data=PAYLOAD, email=_email())
        assert "signature" not in str(caught.value).lower()

    async def test_creates_nothing(self, session, as_telegram) -> None:
        email = _email()
        as_telegram(raises=TelegramAuthError("stale login"))
        with pytest.raises(AuthenticationError):
            await service.login_with_telegram(session, data=PAYLOAD, email=email)

        found = await session.execute(select(Customer).where(Customer.email == email))
        assert found.scalar_one_or_none() is None
