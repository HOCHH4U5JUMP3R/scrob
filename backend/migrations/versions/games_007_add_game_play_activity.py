"""Add historical game play activity.

Revision ID: games_007
Revises: games_006
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa


revision = "games_007"
down_revision = "games_006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "game_play_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("playtime_before_minutes", sa.Integer(), nullable=True),
        sa.Column("playtime_after_minutes", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_game_play_activity_user_played",
        "game_play_activity",
        ["user_id", "played_at"],
    )
    op.create_index(
        "idx_game_play_activity_game_played",
        "game_play_activity",
        ["game_id", "played_at"],
    )
    op.create_index(
        "idx_game_play_activity_platform",
        "game_play_activity",
        ["user_id", "platform"],
    )


def downgrade() -> None:
    op.drop_index("idx_game_play_activity_platform", table_name="game_play_activity")
    op.drop_index("idx_game_play_activity_game_played", table_name="game_play_activity")
    op.drop_index("idx_game_play_activity_user_played", table_name="game_play_activity")
    op.drop_table("game_play_activity")
