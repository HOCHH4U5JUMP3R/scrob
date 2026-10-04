"""Merge the main media migration head with the game statistics branch.

Revision ID: games_008
Revises: games_007, z0a1b2c3d4e5
Create Date: 2026-10-04
"""

from alembic import op


revision = "games_008"
down_revision = ("games_007", "z0a1b2c3d4e5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
