"""Remove delayed media-server echoes of custom-date watch marks.

An outbound Jellyfin/Emby ``mark watched`` has no timestamp parameter.  If a
delayed UserDataSaved webhook escaped the in-memory echo marker, it created a
provisional event at receipt time in addition to the freshly inserted manual
event with the user's historical date.  Keep the manual event and remove only
that narrowly identifiable provisional duplicate.

Revision ID: f0a1b2c3d4e5
Revises: we355created
Create Date: 2026-09-17
"""

from alembic import op


revision = "f0a1b2c3d4e5"
down_revision = "we355created"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM watch_events AS echoed
        USING watch_events AS manual
        WHERE echoed.provisional = true
          AND echoed.completed = true
          AND manual.user_id = echoed.user_id
          AND manual.media_id = echoed.media_id
          AND manual.completed = true
          AND manual.provisional = false
          AND manual.created_at <= echoed.created_at
          AND manual.created_at >= echoed.created_at - INTERVAL '10 minutes'
          AND manual.watched_at IS NOT NULL
          AND manual.watched_at < manual.created_at - INTERVAL '1 minute'
    """)


def downgrade() -> None:
    # Deleted duplicate webhook rows cannot be reconstructed safely.
    pass
