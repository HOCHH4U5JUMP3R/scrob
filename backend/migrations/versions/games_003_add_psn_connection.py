"""Add PlayStation Network connection fields.

Revision ID: games_003
Revises: games_002
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa


revision = "games_003"
down_revision = "games_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("psn_npsso", sa.String(length=255), nullable=True))
    op.add_column("user_settings", sa.Column("psn_online_id", sa.String(length=100), nullable=True))
    op.add_column("user_settings", sa.Column("psn_account_id", sa.String(length=100), nullable=True))
    op.add_column("user_settings", sa.Column("psn_connected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "psn_connected_at")
    op.drop_column("user_settings", "psn_account_id")
    op.drop_column("user_settings", "psn_online_id")
    op.drop_column("user_settings", "psn_npsso")
