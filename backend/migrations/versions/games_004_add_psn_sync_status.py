"""Add PlayStation sync status fields.

Revision ID: games_004
Revises: games_003
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "games_004"
down_revision = "games_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("psn_last_sync_at", sa.DateTime(), nullable=True))
    op.add_column("user_settings", sa.Column("psn_last_sync_count", sa.Integer(), nullable=True))
    op.add_column("user_settings", sa.Column("psn_last_sync_error", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "psn_last_sync_error")
    op.drop_column("user_settings", "psn_last_sync_count")
    op.drop_column("user_settings", "psn_last_sync_at")
