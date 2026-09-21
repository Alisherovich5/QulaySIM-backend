"""The TOTP column that only production had.

0003 wrote out the device table from what a `\\d` on production showed, and the
column list it was read from had quietly dropped `t0` — two characters, filtered
out by the pattern used to read the output. Every environment built from that
revision therefore had a table django-otp would reject rows for, and the counter
arithmetic ignored an offset it is supposed to subtract.

A separate revision rather than a fix to 0003: that one has already run, and
editing an applied migration leaves databases that will never receive the
correction while `alembic current` says they are up to date.

Revision ID: 0004_totp_t0
Revises: 0003_audit_and_totp
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_totp_t0"
down_revision: str | None = "0003_audit_and_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Production already has it; this is for everywhere 0003 built the table.
    op.execute(
        "ALTER TABLE otp_totp_totpdevice ADD COLUMN IF NOT EXISTS t0 bigint NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE otp_totp_totpdevice DROP COLUMN IF EXISTS t0")
