import asyncio
import logging
import secrets
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.xbox import authorization_url, exchange_code, fetch_library
from db import get_db
from dependencies import get_current_user
from models.games import Game, GamePlatform, GameUserStats
from models.users import User, UserSettings
from models.sync import SyncJob, SyncStatus
from db import async_sessionmaker, engine

logger = logging.getLogger(__name__)
router = APIRouter()


class XboxAutoSyncRequest(BaseModel):
    interval: float | None = Field(default=None, ge=0.25, le=48)


class XboxAuthCompletion(BaseModel):
    code_or_url: str


async def _get_settings(db: AsyncSession, user_id: int) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = result.scalar_one_or_none()
    if settings:
        return settings
    settings = UserSettings(user_id=user_id)
    db.add(settings)
    await db.flush()
    return settings


@router.get("/status")
async def xbox_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    return {
        "configured": True,
        "connected": bool(settings.xbox_oauth_token),
        "xuid": settings.xbox_xuid,
        "gamertag": settings.xbox_gamertag,
        "connected_at": settings.xbox_connected_at.isoformat() if settings.xbox_connected_at else None,
        "last_sync_at": settings.xbox_last_sync_at.isoformat() if settings.xbox_last_sync_at else None,
        "last_sync_count": settings.xbox_last_sync_count,
        "last_sync_error": settings.xbox_last_sync_error,
        "auto_sync_interval": settings.xbox_auto_sync_interval,
    }


@router.get("/authorize")
async def xbox_authorize(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    state = secrets.token_urlsafe(32)
    settings = await _get_settings(db, current_user.id)
    settings.xbox_oauth_state = state
    settings.xbox_last_sync_error = None
    await db.commit()
    try:
        return {"authorization_url": authorization_url(state)}
    except Exception as exc:
        settings.xbox_oauth_state = None
        await db.commit()
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/complete")
async def xbox_complete(
    payload: XboxAuthCompletion,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = result.scalar_one_or_none()
    if not settings or not settings.xbox_oauth_state:
        raise HTTPException(status_code=400, detail="No pending Xbox authentication.")

    value = payload.code_or_url.strip()
    code = value
    returned_state = None

    try:
        parsed = urlparse(value)
        if parsed.query:
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            returned_state = params.get("state", [None])[0]
    except ValueError:
        pass

    if not code:
        raise HTTPException(status_code=400, detail="No Xbox authorization code found.")

    if returned_state and returned_state != settings.xbox_oauth_state:
        raise HTTPException(status_code=400, detail="Invalid Xbox OAuth state.")

    try:
        oauth = await exchange_code(code)
        settings.xbox_oauth_token = oauth.model_dump_json()
        settings.xbox_oauth_state = None
        settings.xbox_last_sync_error = None
        await db.commit()
    except Exception as exc:
        logger.warning("Xbox authentication failed: %s", exc)
        settings.xbox_oauth_state = None
        settings.xbox_oauth_token = None
        settings.xbox_last_sync_error = f"Xbox authentication failed: {type(exc).__name__}: {exc}"
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=f"Xbox authentication failed: {type(exc).__name__}: {exc}",
        )

    return {"status": "connected"}


@router.post("/auto-sync")
async def xbox_auto_sync(
    payload: XboxAutoSyncRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    if payload.interval is not None and not settings.xbox_oauth_token:
        raise HTTPException(status_code=400, detail="Xbox is not connected.")
    settings.xbox_auto_sync_interval = payload.interval
    await db.commit()
    return {"auto_sync_interval": settings.xbox_auto_sync_interval}


@router.delete("/disconnect")
async def xbox_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    settings.xbox_oauth_token = None
    settings.xbox_xuid = None
    settings.xbox_gamertag = None
    settings.xbox_connected_at = None
    settings.xbox_last_sync_at = None
    settings.xbox_last_sync_count = None
    settings.xbox_last_sync_error = None
    settings.xbox_oauth_state = None
    settings.xbox_auto_sync_interval = None
    await db.commit()
    return {"status": "disconnected"}


async def _sync_xbox(db: AsyncSession, user_id: int) -> int:
    settings = await _get_settings(db, user_id)
    if not settings.xbox_oauth_token:
        raise RuntimeError("Xbox is not connected.")

    data = await asyncio.wait_for(fetch_library(settings.xbox_oauth_token), timeout=600)
    settings.xbox_xuid = data["xuid"]
    settings.xbox_gamertag = data["gamertag"]
    settings.xbox_connected_at = settings.xbox_connected_at or datetime.utcnow()

    imported = 0
    for item in data["games"]:
        external_id = str(item["title_id"])
        title = (item.get("title") or "Unknown Game").strip()
        platform = item.get("platform") or "xbox"
        platform_name = item.get("platform_name") or "Xbox"

        platform_q = await db.execute(
            select(GamePlatform)
            .options(selectinload(GamePlatform.game))
            .where(GamePlatform.platform == platform, GamePlatform.external_id == external_id)
        )
        game_platform = platform_q.scalar_one_or_none()

        if game_platform:
            game = game_platform.game
        else:
            game_q = await db.execute(select(Game).where(Game.title == title).limit(1))
            game = game_q.scalar_one_or_none()
            if not game:
                game = Game(title=title, cover_path=item.get("cover_path"))
                db.add(game)
                await db.flush()
            game_platform = GamePlatform(
                game_id=game.id,
                platform=platform,
                external_id=external_id,
                platform_name=platform_name,
                external_url="https://www.xbox.com/games/store/",
                metadata_json={"source": "xbox"},
            )
            db.add(game_platform)

        if item.get("cover_path") and not game.cover_path:
            game.cover_path = item["cover_path"]

        stats_q = await db.execute(
            select(GameUserStats).where(
                GameUserStats.user_id == user_id,
                GameUserStats.game_id == game.id,
                GameUserStats.platform == platform,
            )
        )
        stats = stats_q.scalar_one_or_none()
        if not stats:
            stats = GameUserStats(user_id=user_id, game_id=game.id, platform=platform)
            db.add(stats)

        for field in (
            "playtime_minutes", "last_played_at",
            "trophy_progress", "trophies_earned", "trophies_defined",
        ):
            if item.get(field) is not None:
                setattr(stats, field, item[field])
        imported += 1

    settings.xbox_last_sync_at = datetime.utcnow()
    settings.xbox_last_sync_count = imported
    settings.xbox_last_sync_error = None
    await db.commit()
    return imported


async def run_xbox_sync(user_id: int, job_id: int) -> None:
    logger.info("Starting automatic Xbox sync for user %s, job %s", user_id, job_id)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id, SyncJob.status == SyncStatus.pending)
                .values(status=SyncStatus.running, current_step="Syncing Xbox games")
            )
            await db.commit()
            imported = await _sync_xbox(db, user_id)
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(
                    status=SyncStatus.completed,
                    total_items=imported,
                    processed_items=imported,
                    errors=0,
                    current_step="Completed",
                )
            )
            await db.commit()
        except Exception as exc:
            logger.warning("Automatic Xbox sync failed for user %s: %s", user_id, exc)
            settings = await _get_settings(db, user_id)
            settings.xbox_last_sync_error = f"Xbox sync failed: {type(exc).__name__}: {exc}"
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=SyncStatus.failed, errors=1, error_message=settings.xbox_last_sync_error, current_step="Failed")
            )
            await db.commit()


@router.post("/sync")
async def xbox_sync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        imported = await _sync_xbox(db, current_user.id)
    except Exception as exc:
        logger.warning("Xbox sync failed for user %s: %s", current_user.id, exc)
        settings = await _get_settings(db, current_user.id)
        settings.xbox_last_sync_error = f"Xbox sync failed: {type(exc).__name__}: {exc}"
        await db.commit()
        raise HTTPException(status_code=400, detail=settings.xbox_last_sync_error)

    settings = await _get_settings(db, current_user.id)
    return {"status": "synced", "games_imported": imported, "synced_at": settings.xbox_last_sync_at.isoformat()}


@router.get("/games")
async def xbox_games(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Game)
        .options(selectinload(Game.platforms), selectinload(Game.user_stats))
        .join(GameUserStats, GameUserStats.game_id == Game.id)
        .where(
            GameUserStats.user_id == current_user.id,
            GameUserStats.platform.like("xbox%"),
        )
        .order_by(Game.title.asc())
    )
    games = result.scalars().unique().all()
    return {
        "results": [
            {
                "id": game.id,
                "title": game.title,
                "cover_path": game.cover_path,
                "platforms": [
                    {
                        "platform": p.platform,
                        "platform_name": p.platform_name,
                        "external_id": p.external_id,
                    }
                    for p in game.platforms
                ],
                "stats": next(
                    (
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
                        if s.user_id == current_user.id and s.platform.startswith("xbox")
                    ),
                    None,
                ),
            }
            for game in games
        ]
    }
