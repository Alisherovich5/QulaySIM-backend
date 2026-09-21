"""Baseline: everything Django had already created.

Empty on purpose. The live database was built by Django's migrations and is not
being rebuilt; this revision exists so a fresh database and the live one reach
the same version number, and so the first revision that actually creates
something has a parent.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
