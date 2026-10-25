from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete, func

from db import get_db
from models.media import Media
from models.ratings import Rating
from models.base import MediaType
from models.users import UserSettings
from dependencies import get_current_user, get_current_user_or_api_key
from models.users import User
from core.enrichment import enrich_media, create_media_safely

router = APIRouter()


@router.get("/summary")
async def get_rating_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Small API-key-compatible rating count endpoint for Home Assistant."""
    result = await db.execute(
        select(Media.media_type, func.count(Rating.id))
        .join(Media, Media.id == Rating.media_id)
        .where(
            Rating.user_id == current_user.id,
            Rating.rating.isnot(None),
            Media.media_type.in_((MediaType.movie, MediaType.episode)),
        )
        .group_by(Media.media_type)
    )
    counts = dict(result.all())
    movies = counts.get(MediaType.movie, 0)
    episodes = counts.get(MediaType.episode, 0)
    return {
        "rated_movies": movies,
        "rated_episodes": episodes,
        "rated_items": movies + episodes,
    }


class RatingIn(BaseModel):
    tmdb_id: int
    media_type: str
    rating: float = Field(..., ge=0.0, le=10.0)
    review: Optional[str] = None
    season_number: Optional[int] = None
    episode_order: Optional[str] = None


async def _find_media(db: AsyncSession, tmdb_id: int, media_type: MediaType) -> Optional[Media]:
    """Look up a Media row by (tmdb_id, media_type). Duplicate rows for the same
    key exist in the wild - most commonly for episodes, from concurrent webhook/
    sync ingestion racing to create the same one - so this deterministically
    picks the oldest row instead of crashing with MultipleResultsFound (#157)."""
    result = await db.execute(
        select(Media)
        .where(Media.tmdb_id == tmdb_id, Media.media_type == media_type)
        .order_by(Media.id)
    )
    return result.scalars().first()


def format_rating(rating: Rating, media: Media) -> dict:
    return {
        "id": rating.id,
        "media": {
            "id": media.id,
            "tmdb_id": media.tmdb_id,
            "type": media.media_type,
            "title": media.title,
            "poster_path": media.poster_path,
            "release_date": media.release_date,
        },
        "season_number": rating.season_number,
        "episode_order": rating.episode_order,
        "user_id": rating.user_id,
        "rating": rating.rating,
        "review": rating.review,
        "rated_at": rating.rated_at.isoformat(),
    }


async def _recalculate_derived_series_ratings(
    db: AsyncSession,
    user_id: int,
    episode_media_id: int,
) -> tuple[dict[tuple[int, int | None], float], set[tuple[int, int | None]]]:
    """Persist whole-number season and series ratings derived from episode ratings."""
    from models.show import Show

    episode_result = await db.execute(select(Media).where(Media.id == episode_media_id))
    episode_media = episode_result.scalar_one_or_none()
    if not episode_media or not episode_media.show_id:
        return {}, set()

    show_result = await db.execute(select(Show).where(Show.id == episode_media.show_id))
    show = show_result.scalar_one_or_none()
    if not show or not show.tmdb_id:
        return {}, set()

    series_result = await db.execute(
        select(Media)
        .where(Media.tmdb_id == show.tmdb_id, Media.media_type == MediaType.series)
        .order_by(Media.id)
    )
    series_media = series_result.scalars().first()
    if not series_media:
        return {}, set()

    episode_ratings_result = await db.execute(
        select(Media.season_number, Rating.rating)
        .join(Rating, Rating.media_id == Media.id)
        .where(
            Media.show_id == show.id,
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.season_number != 0,
            Rating.user_id == user_id,
            Rating.rating.isnot(None),
        )
    )
    by_season: dict[int, list[float]] = {}
    for season_number, value in episode_ratings_result.all():
        if season_number is not None and value is not None:
            by_season.setdefault(int(season_number), []).append(float(value))

    def whole_rating(values: list[float]) -> float:
        average = sum(values) / len(values)
        return float(min(10, max(1, int(average + 0.5))))

    existing_result = await db.execute(
        select(Rating).where(
            Rating.media_id == series_media.id,
            Rating.user_id == user_id,
            Rating.episode_order.is_(None),
        )
    )
    existing = {row.season_number: row for row in existing_result.scalars().all()}
    changes: dict[tuple[int, int | None], float] = {}
    removed: set[tuple[int, int | None]] = set()
    derived_seasons: list[float] = []

    for season_number in sorted(set(by_season) | {n for n in existing if n is not None}):
        row = existing.get(season_number)
        values = by_season.get(season_number, [])
        if not values:
            if row is not None:
                await db.delete(row)
                removed.add((series_media.id, season_number))
            continue
        rating_value = whole_rating(values)
        derived_seasons.append(rating_value)
        if row is None:
            row = Rating(
                media_id=series_media.id,
                user_id=user_id,
                season_number=season_number,
                episode_order=None,
                rating=rating_value,
            )
            db.add(row)
        elif row.rating != rating_value:
            row.rating = rating_value
            row.rated_at = datetime.utcnow()
        changes[(series_media.id, season_number)] = rating_value

    series_row = existing.get(None)
    if derived_seasons:
        series_rating = whole_rating(derived_seasons)
        if series_row is None:
            series_row = Rating(
                media_id=series_media.id,
                user_id=user_id,
                season_number=None,
                episode_order=None,
                rating=series_rating,
            )
            db.add(series_row)
        elif series_row.rating != series_rating:
            series_row.rating = series_rating
            series_row.rated_at = datetime.utcnow()
        changes[(series_media.id, None)] = series_rating
    elif series_row is not None:
        await db.delete(series_row)
        removed.add((series_media.id, None))

    await db.commit()
    return changes, removed


async def _sync_derived_rating_changes(
    db: AsyncSession,
    user_id: int,
    changes: dict[tuple[int, int | None], float],
    removed: set[tuple[int, int | None]],
) -> None:
    if not changes and not removed:
        return
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    settings = settings_result.scalar_one_or_none()
    from routers.sync import _fan_out_changes_to_other_connections

    await _fan_out_changes_to_other_connections(
        db, user_id, None, set(), changes, settings=settings, removed_ratings=removed
    )


@router.delete("/all")
async def clear_all_ratings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    await db.execute(delete(Rating).where(Rating.user_id == current_user.id))
    await db.commit()
    return {"status": "ok"}


@router.post("")
async def submit_rating(
    body: RatingIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    try:
        media_type = MediaType(body.media_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid media_type: {body.media_type}")

    # Look up existing Media row, create on-the-fly if missing
    media = await _find_media(db, body.tmdb_id, media_type)

    if not media:
        from routers.media import get_user_tmdb_key
        from core import tmdb
        api_key = await get_user_tmdb_key(db, current_user.id)
        try:
            if media_type == MediaType.movie:
                data = await tmdb.get_movie(body.tmdb_id, api_key=api_key)
                title = data.get("title")
            elif media_type == MediaType.series:
                title = None  # enrich_media will populate all fields including title
            else:
                raise HTTPException(status_code=400, detail="Cannot create media row for episodes via rating")
            media, _created = await create_media_safely(db, body.tmdb_id, media_type, title=title or "")
            await enrich_media(media, api_key=api_key)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"TMDB Media not found: {e}")

    effective_season = None if media_type == MediaType.episode else body.season_number
    effective_episode_order = (
        body.episode_order
        if media_type == MediaType.series and effective_season is not None
        else None
    )
    if effective_episode_order not in (None, "tvdb"):
        raise HTTPException(status_code=400, detail="Invalid episode order")

    result2 = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
            Rating.episode_order == effective_episode_order,
        )
    )
    rating = result2.scalar_one_or_none()

    if rating:
        rating.rating = body.rating
        rating.review = body.review
        rating.rated_at = datetime.utcnow()
    else:
        rating = Rating(
            media_id=media.id,
            user_id=current_user.id,
            rating=body.rating,
            review=body.review,
            season_number=effective_season,
            episode_order=effective_episode_order,
        )
        db.add(rating)

    await db.commit()
    await db.refresh(rating)
    if effective_episode_order == "tvdb":
        return format_rating(rating, media)

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()
    from routers.sync import _fan_out_changes_to_other_connections

    await _fan_out_changes_to_other_connections(
        db,
        current_user.id,
        None,
        set(),
        {(media.id, effective_season): body.rating},
        settings=settings,
    )

    if media_type == MediaType.episode:
        derived_changes, derived_removed = await _recalculate_derived_series_ratings(
            db, current_user.id, media.id
        )
        await _sync_derived_rating_changes(
            db, current_user.id, derived_changes, derived_removed
        )


    return format_rating(rating, media)


@router.get("")
async def get_ratings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    result = await db.execute(
        select(Rating, Media)
        .join(Media, Media.id == Rating.media_id)
        .where(Rating.user_id == current_user.id)
        .order_by(desc(Rating.rated_at))
    )
    return {"results": [format_rating(r, m) for r, m in result.all()]}


@router.get("/{media_id}")
async def get_media_rating(
    media_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    result = await db.execute(
        select(Rating, Media)
        .join(Media, Media.id == Rating.media_id)
        .where(Rating.media_id == media_id, Rating.user_id == current_user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Rating not found")
    return format_rating(row[0], row[1])


@router.delete("")
async def delete_rating(
    tmdb_id: int,
    media_type: str,
    season_number: Optional[int] = Query(None),
    episode_order: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    try:
        mt = MediaType(media_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid media_type: {media_type}")

    media = await _find_media(db, tmdb_id, mt)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    effective_season = None if mt == MediaType.episode else season_number
    effective_episode_order = (
        episode_order
        if mt == MediaType.series and effective_season is not None
        else None
    )

    result = await db.execute(
        select(Rating).where(
            Rating.media_id == media.id,
            Rating.user_id == current_user.id,
            Rating.season_number == effective_season,
            Rating.episode_order == effective_episode_order,
        )
    )
    rating = result.scalar_one_or_none()
    if not rating:
        raise HTTPException(status_code=404, detail="Rating not found")
    await db.delete(rating)
    await db.commit()
    if effective_episode_order == "tvdb":
        return {"status": "deleted"}

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()
    from routers.sync import _fan_out_changes_to_other_connections

    await _fan_out_changes_to_other_connections(
        db,
        current_user.id,
        None,
        set(),
        {},
        settings=settings,
        removed_ratings={(media.id, effective_season)},
    )

    if mt == MediaType.episode:
        derived_changes, derived_removed = await _recalculate_derived_series_ratings(
            db, current_user.id, media.id
        )
        await _sync_derived_rating_changes(
            db, current_user.id, derived_changes, derived_removed
        )


    return {"status": "deleted"}
