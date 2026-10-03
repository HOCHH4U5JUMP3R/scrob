"""Add automatic game sync intervals.

Revision ID: games_006
Revises: games_005
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "games_006"
down_revision = "games_005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("psn_auto_sync_interval", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_auto_sync_interval", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "xbox_auto_sync_interval")
    op.drop_column("user_settings", "psn_auto_sync_interval")
