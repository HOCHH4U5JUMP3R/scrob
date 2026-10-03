from datetime import datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        Index("idx_games_title", "title"),
        Index("idx_games_release_date", "release_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[Optional[str]] = mapped_column(String(500))
    slug: Mapped[Optional[str]] = mapped_column(String(500))
    overview: Mapped[Optional[str]] = mapped_column(Text)
    cover_path: Mapped[Optional[str]] = mapped_column(String(500))
    backdrop_path: Mapped[Optional[str]] = mapped_column(String(500))
    release_date: Mapped[Optional[Date]] = mapped_column(Date)
    genres: Mapped[Optional[list]] = mapped_column(JSONB)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    platforms: Mapped[list["GamePlatform"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
    )
    user_stats: Mapped[list["GameUserStats"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
    )


class GamePlatform(Base):
    __tablename__ = "game_platforms"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_id",
            name="uq_game_platform_external",
        ),
        Index("idx_game_platforms_game", "game_id"),
        Index("idx_game_platforms_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_name: Mapped[Optional[str]] = mapped_column(String(100))
    external_url: Mapped[Optional[str]] = mapped_column(String(500))
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)

    game: Mapped["Game"] = relationship(back_populates="platforms")


class GameUserStats(Base):
    __tablename__ = "game_user_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "game_id",
            "platform",
            name="uq_game_user_stats_user_game_platform",
        ),
        Index("idx_game_user_stats_user", "user_id"),
        Index("idx_game_user_stats_game", "game_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    play_count: Mapped[Optional[int]] = mapped_column(Integer)
    playtime_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    first_played_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_played_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    trophy_progress: Mapped[Optional[int]] = mapped_column(Integer)
    trophies_earned: Mapped[Optional[dict]] = mapped_column(JSONB)
    trophies_defined: Mapped[Optional[dict]] = mapped_column(JSONB)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    game: Mapped["Game"] = relationship(back_populates="user_stats")
