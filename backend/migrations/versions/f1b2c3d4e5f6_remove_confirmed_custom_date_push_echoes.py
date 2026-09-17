"""Remove historic custom-date push echoes that were stored as confirmed.

Some media-server sync paths stored a push echo as a regular (non-provisional)
WatchEvent.  Those rows were not covered by the earlier provisional-only
cleanup, so a manually backdated episode could remain counted both on its
chosen watch date and on the date that it was marked watched in Scrob.

The pair is deliberately identified by its insertion-time signature: the
manual event is backdated, while the second event for the same user and media
was created within ten minutes and is timestamped at that insertion time.
That preserves legitimate historic rewatches with their own chosen dates.

Revision ID: f1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-09-17
"""

from alembic import op


revision = "f1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM watch_events AS echoed
        USING watch_events AS manual
        WHERE echoed.id <> manual.id
          AND echoed.completed = true
          AND manual.user_id = echoed.user_id
          AND manual.media_id = echoed.media_id
          AND manual.completed = true
          AND manual.watched_at IS NOT NULL
          AND manual.watched_at < manual.created_at - INTERVAL '1 minute'
          AND echoed.created_at >= manual.created_at
          AND echoed.created_at <= manual.created_at + INTERVAL '10 minutes'
          AND echoed.watched_at >= manual.created_at - INTERVAL '1 minute'
          AND echoed.watched_at <= echoed.created_at + INTERVAL '1 minute'
    """)


def downgrade() -> None:
    # Deleted duplicate webhook/sync rows cannot be reconstructed safely.
    pass
