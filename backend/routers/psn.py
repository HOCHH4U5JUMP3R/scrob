import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.psn import fetch_library
from db import get_db
from dependencies import get_current_user
from models.games import Game, GamePlatform, GameUserStats
from models.users import User, UserSettings
from models.sync import SyncJob, SyncStatus
from db import async_sessionmaker, engine

logger = logging.getLogger(__name__)
router = APIRouter()


class PSNConnectRequest(BaseModel):
    npsso: str = Field(min_length=20, max_length=255)


def _serialize_stats(stats: GameUserStats | None) -> dict | None:
    if not stats:
        return None
    return {
        "platform": stats.platform,
        "play_count": stats.play_count,
        "playtime_minutes": stats.playtime_minutes,
        "first_played_at": stats.first_played_at.isoformat() if stats.first_played_at else None,
        "last_played_at": stats.last_played_at.isoformat() if stats.last_played_at else None,
        "trophy_progress": stats.trophy_progress,
        "trophies_earned": stats.trophies_earned,
        "trophies_defined": stats.trophies_defined,
    }


def _platform_slug(platform_name: str) -> str:
    name = platform_name.lower()
    if "5" in name:
        return "playstation-5"
    if "4" in name:
        return "playstation-4"
    if "vita" in name:
        return "playstation-vita"
    if "3" in name:
        return "playstation-3"
    if "pc" in name:
        return "playstation-pc"
    return "playstation"


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
async def psn_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    return {
        "connected": bool(settings.psn_npsso),
        "online_id": settings.psn_online_id,
        "account_id": settings.psn_account_id,
        "connected_at": settings.psn_connected_at.isoformat() if settings.psn_connected_at else None,
        "last_sync_at": settings.psn_last_sync_at.isoformat() if settings.psn_last_sync_at else None,
        "last_sync_count": settings.psn_last_sync_count,
        "last_sync_error": settings.psn_last_sync_error,
    }


class PSNAutoSyncRequest(BaseModel):
    interval: float | None = Field(default=None, ge=0.25, le=48)


@router.post("/connect")
async def psn_connect(
    payload: PSNConnectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    npsso = payload.npsso.strip()
    # Connection validation deliberately does not fetch the complete game
    # library. The library endpoints can be large and occasionally rate-limit
    # independently of authentication. A successful connection should therefore
    # only prove that the NPSSO can authenticate; syncing is a separate action.
    try:
        from psnawp_api import PSNAWP

        client = await asyncio.to_thread(lambda: PSNAWP(npsso).me())
        online_id = client.online_id
        account_id = client.account_id
    except Exception as exc:
        logger.warning("PSN authentication failed for user %s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=400,
            detail=f"PlayStation authentication failed: {type(exc).__name__}: {exc}",
        )

    settings = await _get_settings(db, current_user.id)
    settings.psn_npsso = npsso
    settings.psn_online_id = online_id
    settings.psn_account_id = account_id
    settings.psn_connected_at = settings.psn_connected_at or datetime.utcnow()
    settings.psn_last_sync_error = None
    await db.commit()

    return {
        "status": "connected",
        "online_id": online_id,
        "account_id": account_id,
    }


@router.post("/auto-sync")
async def psn_auto_sync(
    payload: PSNAutoSyncRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    if payload.interval is not None and not settings.psn_npsso:
        raise HTTPException(status_code=400, detail="PlayStation is not connected.")
    settings.psn_auto_sync_interval = payload.interval
    await db.commit()
    return {"auto_sync_interval": settings.psn_auto_sync_interval}


@router.delete("/disconnect")
async def psn_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    settings.psn_npsso = None
    settings.psn_online_id = None
    settings.psn_account_id = None
    settings.psn_connected_at = None
    settings.psn_last_sync_at = None
    settings.psn_last_sync_count = None
    settings.psn_last_sync_error = None
    await db.commit()
    return {"status": "disconnected"}


async def _sync_psn(db: AsyncSession, user_id: int) -> int:
    settings = await _get_settings(db, user_id)
    if not settings.psn_npsso:
        raise RuntimeError("PlayStation is not connected.")

    data = await asyncio.to_thread(fetch_library, settings.psn_npsso)
    settings.psn_online_id = data["online_id"]
    settings.psn_account_id = data["account_id"]
    settings.psn_connected_at = settings.psn_connected_at or datetime.utcnow()

    imported = 0
    for item in data["games"]:
        external_id = str(item["title_id"])
        title = (item.get("title") or "Unknown Game").strip()
        platform_name = item.get("platform_name") or "PlayStation"
        platform = _platform_slug(platform_name)

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
                external_url="https://store.playstation.com/",
                metadata_json={"source": "psn"},
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
            "play_count", "playtime_minutes", "first_played_at",
            "last_played_at", "trophy_progress", "trophies_earned",
            "trophies_defined",
        ):
            # A missing/None provider value must never erase a previously known statistic.
            if item.get(field) is not None:
                setattr(stats, field, item[field])
        imported += 1

    settings.psn_last_sync_at = datetime.utcnow()
    settings.psn_last_sync_count = imported
    settings.psn_last_sync_error = None
    await db.commit()
    return imported


async def run_psn_sync(user_id: int, job_id: int) -> None:
    logger.info("Starting automatic PlayStation sync for user %s, job %s", user_id, job_id)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as db:
        try:
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id, SyncJob.status == SyncStatus.pending)
                .values(status=SyncStatus.running, current_step="Syncing PlayStation games")
            )
            await db.commit()
            imported = await _sync_psn(db, user_id)
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
            logger.warning("Automatic PSN sync failed for user %s: %s", user_id, exc)
            settings = await _get_settings(db, user_id)
            settings.psn_last_sync_error = f"PlayStation sync failed: {type(exc).__name__}: {exc}"
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=SyncStatus.failed, errors=1, error_message=settings.psn_last_sync_error, current_step="Failed")
            )
            await db.commit()


@router.post("/sync")
async def psn_sync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        imported = await _sync_psn(db, current_user.id)
    except Exception as exc:
        logger.warning("PSN sync failed for user %s: %s", current_user.id, exc)
        settings = await _get_settings(db, current_user.id)
        settings.psn_last_sync_error = f"PlayStation sync failed: {type(exc).__name__}: {exc}"
        await db.commit()
        raise HTTPException(status_code=400, detail=settings.psn_last_sync_error)

    settings = await _get_settings(db, current_user.id)
    return {"status": "synced", "games_imported": imported, "synced_at": settings.psn_last_sync_at.isoformat()}


@router.get("/games")
async def psn_games(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Game)
        .options(selectinload(Game.platforms), selectinload(Game.user_stats))
        .join(GameUserStats, GameUserStats.game_id == Game.id)
        .where(
            GameUserStats.user_id == current_user.id,
            GameUserStats.platform.like("playstation%"),
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
                "stats": _serialize_stats(
                    next(
                        (s for s in game.user_stats if s.user_id == current_user.id and s.platform.startswith("playstation")),
                        None,
                    )
                ),
            }
            for game in games
        ]
    }
