"""Add canonical games and external platform identities.

Revision ID: games_001
Revises: f1b2c3d4e5f6
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "games_001"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("original_title", sa.String(length=500), nullable=True),
        sa.Column("slug", sa.String(length=500), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("cover_path", sa.String(length=500), nullable=True),
        sa.Column("backdrop_path", sa.String(length=500), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("genres", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_games_title", "games", ["title"])
    op.create_index("idx_games_release_date", "games", ["release_date"])

    op.create_table(
        "game_platforms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("platform_name", sa.String(length=100), nullable=True),
        sa.Column("external_url", sa.String(length=500), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("platform", "external_id", name="uq_game_platform_external"),
    )
    op.create_index("idx_game_platforms_game", "game_platforms", ["game_id"])
    op.create_index("idx_game_platforms_platform", "game_platforms", ["platform"])


def downgrade() -> None:
    op.drop_index("idx_game_platforms_platform", table_name="game_platforms")
    op.drop_index("idx_game_platforms_game", table_name="game_platforms")
    op.drop_table("game_platforms")
    op.drop_index("idx_games_release_date", table_name="games")
    op.drop_index("idx_games_title", table_name="games")
    op.drop_table("games")
