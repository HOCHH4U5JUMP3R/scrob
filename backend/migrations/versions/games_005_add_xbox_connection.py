"""Add Xbox connection and sync fields.

Revision ID: games_005
Revises: games_004
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "games_005"
down_revision = "games_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("xbox_oauth_token", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_oauth_state", sa.String(length=255), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_xuid", sa.String(length=100), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_gamertag", sa.String(length=100), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_connected_at", sa.DateTime(), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_last_sync_at", sa.DateTime(), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_last_sync_count", sa.Integer(), nullable=True))
    op.add_column("user_settings", sa.Column("xbox_last_sync_error", sa.String(length=500), nullable=True))


def downgrade() -> None:
    for name in (
        "xbox_last_sync_error",
        "xbox_last_sync_count",
        "xbox_last_sync_at",
        "xbox_connected_at",
        "xbox_gamertag",
        "xbox_xuid",
        "xbox_oauth_state",
        "xbox_oauth_token",
    ):
        op.drop_column("user_settings", name)
