from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, func, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Show(Base):
    __tablename__ = "shows"
    __table_args__ = (
        UniqueConstraint("jellyfin_connection_id", "jellyfin_source_id", name="uq_shows_jellyfin_source"),
        Index("idx_shows_jellyfin_source", "jellyfin_connection_id", "jellyfin_source_id"),
    )

    id             : Mapped[int]             = mapped_column(Integer, primary_key=True)
    tmdb_id        : Mapped[Optional[int]]    = mapped_column(Integer, unique=True, nullable=True)
    tvdb_id        : Mapped[Optional[int]]    = mapped_column(Integer, unique=True, nullable=True)
    # Jellyfin's stable library id lets shows without an external provider id
    # remain linked across later syncs.
    jellyfin_connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    jellyfin_source_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title          : Mapped[str]             = mapped_column(String(500), nullable=False)
    original_title : Mapped[Optional[str]]   = mapped_column(String(500))
    overview       : Mapped[Optional[str]]   = mapped_column(Text)
    poster_path    : Mapped[Optional[str]]   = mapped_column(String(500))
    backdrop_path  : Mapped[Optional[str]]   = mapped_column(String(500))
    tmdb_rating    : Mapped[Optional[float]] = mapped_column(Float)
    status         : Mapped[Optional[str]]   = mapped_column(String(100))
    tagline        : Mapped[Optional[str]]   = mapped_column(Text)
    first_air_date : Mapped[Optional[str]]   = mapped_column(String(20))
    last_air_date  : Mapped[Optional[str]]   = mapped_column(String(20))
    tmdb_data      : Mapped[Optional[dict]]  = mapped_column(JSONB)
    created_at     : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at     : Mapped[datetime]        = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    episodes : Mapped[list["Media"]] = relationship(back_populates="show")
