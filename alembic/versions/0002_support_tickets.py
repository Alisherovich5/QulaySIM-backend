"""Keep support messages instead of only forwarding them.

Until now the contact form posted straight to Telegram and nothing was stored.
That is fine until somebody writes twice, or writes on a Sunday, or the person
who saw the message is not the person who can answer it — at which point there
is no record that it ever arrived.

Two tables. A ticket is one person's request; notes are what staff wrote on it.
Replies to the customer are NOT modelled, because there is no outbound email in
this system and a table full of answers nobody sent would be worse than none.

Revision ID: 0002_support_tickets
Revises: 0001_baseline
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002_support_tickets"
down_revision: str | None = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_ticket",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("name", sa.String(120), nullable=False, server_default=""),
        sa.Column("email", sa.String(254), nullable=False, server_default=""),
        sa.Column("phone", sa.String(40), nullable=False, server_default=""),
        sa.Column("locale", sa.String(8), nullable=False, server_default="uz"),
        sa.Column("message", sa.Text, nullable=False, server_default=""),
        sa.Column("client_ip", sa.String(45), nullable=False, server_default=""),
        # new → seen → closed. Kept as text rather than an enum so adding a
        # state later is a code change, not a migration against a live table.
        sa.Column("state", sa.String(12), nullable=False, server_default="new"),
        sa.Column(
            "customer_id",
            sa.BigInteger,
            sa.ForeignKey("customers_customer.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assignee_id",
            sa.Integer,
            sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The list is always "newest first, optionally one state".
    op.create_index("ix_support_ticket_state_created", "support_ticket", ["state", "created_at"])
    op.create_index("ix_support_ticket_email", "support_ticket", ["email"])

    op.create_table(
        "support_note",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "ticket_id",
            sa.BigInteger,
            sa.ForeignKey("support_ticket.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.Integer,
            sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_support_note_ticket", "support_note", ["ticket_id", "created_at"])


def downgrade() -> None:
    op.drop_table("support_note")
    op.drop_table("support_ticket")
