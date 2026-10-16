from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db import get_db
from dependencies import get_current_user_or_api_key
from models.game_play_activity import GamePlayActivity
from models.games import Game, GameUserStats
from models.users import User

router = APIRouter()


def _game(game: Game, current_user_id: int) -> dict:
    return {
        "id": game.id,
        "title": game.title,
        "original_title": game.original_title,
        "slug": game.slug,
        "overview": game.overview,
        "cover_path": game.cover_path,
        "backdrop_path": game.backdrop_path,
        "release_date": game.release_date.isoformat() if game.release_date else None,
        "genres": game.genres or [],
        "platforms": [
            {
                "id": p.id,
                "platform": p.platform,
                "external_id": p.external_id,
                "platform_name": p.platform_name,
                "external_url": p.external_url,
            }
            for p in game.platforms
        ],
        "stats": [
            {
                "platform": s.platform,
                "play_count": s.play_count,
                "playtime_minutes": s.playtime_minutes,
                "first_played_at": s.first_played_at.isoformat() if s.first_played_at else None,
                "last_played_at": s.last_played_at.isoformat() if s.last_played_at else None,
                "trophy_progress": s.trophy_progress,
                "trophies_earned": s.trophies_earned,
                "trophies_defined": s.trophies_defined,
            }
            for s in game.user_stats
            if s.user_id == current_user_id
        ],
    }


@router.get("")
async def list_games(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    result = await db.execute(
        select(Game)
        .options(selectinload(Game.platforms), selectinload(Game.user_stats))
        .order_by(Game.title.asc())
    )
    return {"results": [_game(game, current_user.id) for game in result.scalars().unique().all()]}




@router.delete("/library")
async def delete_game_library(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Remove all game statistics owned by the current user and clean up orphaned games."""
    deleted_stats = await db.execute(
        delete(GameUserStats).where(GameUserStats.user_id == current_user.id)
    )

    orphan_result = await db.execute(
        select(Game).where(~Game.user_stats.any())
    )
    orphaned_games = orphan_result.scalars().all()
    for game in orphaned_games:
        await db.delete(game)

    await db.commit()
    return {
        "deleted_stats": deleted_stats.rowcount or 0,
        "deleted_games": len(orphaned_games),
    }


@router.post("/{game_id}/activity")
async def create_game_activity(
    game_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Manually add a play activity for the current user."""
    game = await db.get(Game, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    platform = str(payload.get("platform") or "").strip()
    if not platform or len(platform) > 32:
        raise HTTPException(status_code=422, detail="A valid platform is required")
    try:
        duration = int(payload.get("duration_minutes", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Duration must be a whole number")
    if duration < 0 or duration > 1000000:
        raise HTTPException(status_code=422, detail="Duration is out of range")
    try:
        raw_date = payload.get("played_at")
        played_at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")) if raw_date else datetime.now(timezone.utc)
        if played_at.tzinfo is not None:
            played_at = played_at.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid date or time")
    activity = GamePlayActivity(
        user_id=current_user.id, game_id=game_id, platform=platform,
        played_at=played_at, duration_minutes=duration, source="manual",
    )
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return {"id": activity.id, "game_id": activity.game_id, "platform": activity.platform,
            "played_at": activity.played_at.isoformat() if activity.played_at else None,
            "duration_minutes": activity.duration_minutes, "observed_at": activity.observed_at.isoformat(),
            "source": activity.source}


@router.patch("/activity/{activity_id}")
async def update_game_activity(
    activity_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Edit a play activity owned by the current user."""
    result = await db.execute(select(GamePlayActivity).where(
        GamePlayActivity.id == activity_id,
        GamePlayActivity.user_id == current_user.id,
    ))
    activity = result.scalar_one_or_none()
    if activity is None:
        raise HTTPException(status_code=404, detail="Play activity not found")
    if "platform" in payload:
        platform = str(payload["platform"] or "").strip()
        if not platform or len(platform) > 32:
            raise HTTPException(status_code=422, detail="A valid platform is required")
        activity.platform = platform
    if "duration_minutes" in payload:
        try:
            duration = int(payload["duration_minutes"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Duration must be a whole number")
        if duration < 0 or duration > 1000000:
            raise HTTPException(status_code=422, detail="Duration is out of range")
        activity.duration_minutes = duration
    if "played_at" in payload:
        try:
            value = payload["played_at"]
            activity.played_at = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
            if activity.played_at and activity.played_at.tzinfo is not None:
                activity.played_at = activity.played_at.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid date or time")
    await db.commit()
    await db.refresh(activity)
    return {"id": activity.id, "game_id": activity.game_id, "platform": activity.platform,
            "played_at": activity.played_at.isoformat() if activity.played_at else None,
            "duration_minutes": activity.duration_minutes, "observed_at": activity.observed_at.isoformat(),
            "source": activity.source}

@router.get("/{game_id}")
async def get_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    result = await db.execute(
        select(Game)
        .options(selectinload(Game.platforms), selectinload(Game.user_stats))
        .where(Game.id == game_id)
    )
    game = result.scalar_one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    payload = _game(game, current_user.id)
    activity_result = await db.execute(
        select(GamePlayActivity)
        .where(
            GamePlayActivity.game_id == game_id,
            GamePlayActivity.user_id == current_user.id,
        )
        .order_by(GamePlayActivity.played_at.desc(), GamePlayActivity.id.desc())
    )
    payload["play_history"] = [
        {
            "id": activity.id,
            "game_id": activity.game_id,
            "platform": activity.platform,
            "played_at": activity.played_at.isoformat() if activity.played_at else None,
            "duration_minutes": activity.duration_minutes,
            "playtime_before_minutes": activity.playtime_before_minutes,
            "playtime_after_minutes": activity.playtime_after_minutes,
            "observed_at": activity.observed_at.isoformat() if activity.observed_at else None,
            "source": activity.source,
        }
        for activity in activity_result.scalars().all()
    ]
    return payload
