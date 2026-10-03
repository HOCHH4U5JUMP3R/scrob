"""Add per-user game statistics.

Revision ID: games_002
Revises: games_001
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "games_002"
down_revision = "games_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "game_user_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("play_count", sa.Integer(), nullable=True),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("first_played_at", sa.DateTime(), nullable=True),
        sa.Column("last_played_at", sa.DateTime(), nullable=True),
        sa.Column("trophy_progress", sa.Integer(), nullable=True),
        sa.Column("trophies_earned", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trophies_defined", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "game_id", "platform",
            name="uq_game_user_stats_user_game_platform",
        ),
    )
    op.create_index("idx_game_user_stats_user", "game_user_stats", ["user_id"])
    op.create_index("idx_game_user_stats_game", "game_user_stats", ["game_id"])


def downgrade() -> None:
    op.drop_index("idx_game_user_stats_game", table_name="game_user_stats")
    op.drop_index("idx_game_user_stats_user", table_name="game_user_stats")
    op.drop_table("game_user_stats")
