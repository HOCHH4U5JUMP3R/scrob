from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GamePlayActivity(Base):
    """Recorded game activity, including user-created and user-corrected rows.

    Platforms expose cumulative playtime rather than exact session boundaries.
    Each sync records only the newly observed playtime delta. The before/after
    values preserve that original delta so edits to a synced activity can be
    reflected in derived statistics without changing the provider baseline.
    """

    __tablename__ = "game_play_activity"
    __table_args__ = (
        Index("idx_game_play_activity_user_played", "user_id", "played_at"),
        Index("idx_game_play_activity_game_played", "game_id", "played_at"),
        Index("idx_game_play_activity_platform", "user_id", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    played_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    playtime_before_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    playtime_after_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
