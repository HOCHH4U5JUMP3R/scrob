"""Persist Jellyfin show identities for metadata-first archival syncs.

Revision ID: jf_metadata_archive_001
Revises: we358series_drop_series_watch_events
"""

from alembic import op
import sqlalchemy as sa

revision = "jf_metadata_archive_001"
down_revision = "we358series_drop_series_watch_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shows", sa.Column("jellyfin_connection_id", sa.Integer(), nullable=True))
    op.add_column("shows", sa.Column("jellyfin_source_id", sa.String(length=255), nullable=True))
    op.create_unique_constraint(
        "uq_shows_jellyfin_source", "shows", ["jellyfin_connection_id", "jellyfin_source_id"]
    )
    op.create_index(
        "idx_shows_jellyfin_source", "shows", ["jellyfin_connection_id", "jellyfin_source_id"]
    )


def downgrade():
    op.drop_index("idx_shows_jellyfin_source", table_name="shows")
    op.drop_constraint("uq_shows_jellyfin_source", "shows", type_="unique")
    op.drop_column("shows", "jellyfin_source_id")
    op.drop_column("shows", "jellyfin_connection_id")
