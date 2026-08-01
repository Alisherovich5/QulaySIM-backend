"""Signing in with Google, against the real Django-owned schema.

The interesting cases are all about *linking*: which customer a Google identity
resolves to, and what happens when the answer is ambiguous. They need the real
unique constraints, so they run against Postgres rather than a stub.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, select

from app.core.errors import AuthenticationError, ConflictError, ServiceUnavailableError
from app.core.security import hash_password, verify_password
from app.db.models import Customer, SocialAccount
from app.db.session import SessionFactory, engine
from app.integrations import google_auth
from app.integrations.google_auth import GoogleIdentity
from app.services import auth as service

# The service verifies the credential before it links anything; these tests are
# about the linking, so the verifier is stood in for. Its own checks are covered
# exhaustively in tests/unit/test_google_auth.py.
CREDENTIAL = "stand-in-credential-long-enough-to-pass-the-length-check"


@pytest.fixture
async def session():
    async with SessionFactory() as s:
        yield s
        await s.rollback()


@pytest.fixture
def as_google(monkeypatch):
    """Make the verifier assert a given identity, or fail in a given way."""

    def _install(identity: GoogleIdentity | None = None, *, raises: Exception | None = None):
        async def _verify(_credential: str) -> GoogleIdentity:
            if raises is not None:
                raise raises
            assert identity is not None
            return identity

        # Patched where the service imports it from, not where it is defined.
        monkeypatch.setattr(google_auth, "verify_id_token", _verify)

    return _install


def _identity(email: str, subject: str | None = None) -> GoogleIdentity:
    return GoogleIdentity(
        subject=subject or f"sub-{uuid.uuid4().hex}",
        email=email,
        full_name="Google Signer",
    )


def _email() -> str:
    return f"google-{uuid.uuid4().hex[:12]}@example.com"


async def _existing_customer(session, email: str, *, password: str, active: bool = True):
    customer = Customer(
        email=email,
        full_name="Password Signer",
        hashed_password=hash_password(password),
        is_active=active,
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)
    return customer


async def _links(session, customer_id: int) -> list[SocialAccount]:
    rows = await session.execute(
        select(SocialAccount).where(SocialAccount.customer_id == customer_id)
    )
    return list(rows.scalars().all())


class TestFirstSignIn:
    async def test_an_unknown_identity_creates_a_passwordless_customer(self, session, as_google):
        identity = _identity(_email())
        as_google(identity)

        customer = await service.login_with_google(session, credential=CREDENTIAL)

        assert customer.email == identity.email
        # Empty hash, and verify_password refuses one — so the account cannot be
        # entered with a guessed password just because it has no password.
        assert customer.hashed_password == ""
        assert not verify_password("anything-at-all", customer.hashed_password)
        assert customer.referral_code

    async def test_the_same_identity_signs_into_the_same_customer(self, session, as_google):
        identity = _identity(_email())
        as_google(identity)

        first = await service.login_with_google(session, credential=CREDENTIAL)
        second = await service.login_with_google(session, credential=CREDENTIAL)

        assert first.id == second.id
        assert len(await _links(session, first.id)) == 1


class TestLinkingAnExistingAccount:
    async def test_a_matching_password_account_is_linked_not_duplicated(self, session, as_google):
        """The question this module exists to answer: an address that already
        registered with a password does not blow up on the unique index, it
        becomes the same account with a Google link attached."""
        email = _email()
        existing = await _existing_customer(session, email, password="a-long-enough-password")
        as_google(_identity(email))

        customer = await service.login_with_google(session, credential=CREDENTIAL)

        assert customer.id == existing.id
        links = await _links(session, existing.id)
        assert [link.provider for link in links] == ["google"]

    async def test_linking_leaves_the_password_working(self, session, as_google):
        email = _email()
        await _existing_customer(session, email, password="a-long-enough-password")
        as_google(_identity(email))

        await service.login_with_google(session, credential=CREDENTIAL)

        signed_in = await service.authenticate(
            session, email=email, password="a-long-enough-password"
        )
        assert signed_in.email == email

    async def test_the_address_is_matched_case_insensitively(self, session, as_google):
        email = _email()
        existing = await _existing_customer(session, email, password="a-long-enough-password")
        # verify_id_token lowercases, but the customer row is matched on
        # lower(email) too — a second account for the same person is the one
        # outcome nobody can unpick later.
        as_google(_identity(email.upper().lower()))

        customer = await service.login_with_google(session, credential=CREDENTIAL)
        assert customer.id == existing.id

    async def test_a_second_google_identity_on_one_account_is_a_clean_conflict(
        self, session, as_google
    ):
        """Regression: this used to recurse until RecursionError, i.e. a 500.

        Django enforces one link per customer per provider. A Google account
        deleted and recreated on the same address comes back with a new `sub`,
        which lands here — retrying can never resolve it, so it has to be said
        once.
        """
        email = _email()
        as_google(_identity(email, subject=f"sub-first-{uuid.uuid4().hex}"))
        first = await service.login_with_google(session, credential=CREDENTIAL)
        # Read before the failing call: the rollback inside it expires the
        # instance, and re-loading an expired attribute is IO in a sync context.
        first_id = first.id

        as_google(_identity(email, subject=f"sub-second-{uuid.uuid4().hex}"))
        with pytest.raises(ConflictError):
            await service.login_with_google(session, credential=CREDENTIAL)

        # And the original link is untouched.
        assert len(await _links(session, first_id)) == 1


class TestRefusals:
    async def test_a_disabled_account_cannot_be_entered_through_google(self, session, as_google):
        email = _email()
        await _existing_customer(
            session, email, password="a-long-enough-password", active=False
        )
        as_google(_identity(email))

        with pytest.raises(AuthenticationError):
            await service.login_with_google(session, credential=CREDENTIAL)

    async def test_a_disabled_account_cannot_be_entered_through_an_existing_link(
        self, session, as_google
    ):
        email = _email()
        identity = _identity(email)
        as_google(identity)
        customer = await service.login_with_google(session, credential=CREDENTIAL)

        customer.is_active = False
        await session.commit()

        with pytest.raises(AuthenticationError):
            await service.login_with_google(session, credential=CREDENTIAL)

    async def test_a_rejected_token_is_a_401_not_a_500(self, session, as_google):
        as_google(raises=google_auth.GoogleAuthError("Token rejected: signature"))

        with pytest.raises(AuthenticationError):
            await service.login_with_google(session, credential=CREDENTIAL)

    async def test_an_unreachable_google_is_a_503_not_a_401(self, session, as_google):
        # The customer's account is fine; ours is the side that is broken.
        as_google(raises=google_auth.GoogleUnavailableError("googleapis is not answering"))

        with pytest.raises(ServiceUnavailableError):
            await service.login_with_google(session, credential=CREDENTIAL)


class TestSchemaContract:
    """The linking logic leans on two Django constraints. If a migration drops
    one, the duplicate-link branch silently starts creating duplicates instead
    of raising, and `scalar_one_or_none` below it starts raising 500s."""

    async def test_the_link_uniqueness_constraints_exist(self) -> None:
        async with engine.connect() as conn:
            indexes = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_indexes("customers_socialaccount")
            )
            constraints = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_unique_constraints(
                    "customers_socialaccount"
                )
            )

        unique_column_sets = {
            tuple(sorted(entry["column_names"]))
            for entry in [*constraints, *(i for i in indexes if i.get("unique"))]
        }
        assert ("provider", "provider_uid") in unique_column_sets, (
            "one_link_per_provider_identity is missing: two customers could claim "
            "the same Google account."
        )
        assert ("customer_id", "provider") in unique_column_sets, (
            "one_link_per_customer_per_provider is missing."
        )
