import asyncio
import logging
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.psn import authenticate, fetch_library
from db import get_db
from dependencies import get_current_user
from models.games import Game, GamePlatform, GameUserStats
from models.users import User, UserSettings

logger = logging.getLogger(__name__)
router = APIRouter()

_browser_logins: dict[str, dict] = {}


def _psn_auth_from_settings(settings: UserSettings) -> dict | None:
    preferences = settings.preferences or {}
    auth = preferences.get("psn_auth")
    return auth if isinstance(auth, dict) and auth.get("refresh_token") else None


def _store_psn_auth(settings: UserSettings, token_response: dict) -> None:
    preferences = dict(settings.preferences or {})
    preferences["psn_auth"] = {
        "refresh_token": token_response.get("refresh_token"),
        "refresh_token_expires_in": token_response.get("refresh_token_expires_in"),
        "refresh_token_expires_at": token_response.get("refresh_token_expires_at"),
    }
    settings.preferences = preferences


def _prune_browser_logins() -> None:
    cutoff = time.time() - 600
    for state, pending in list(_browser_logins.items()):
        if pending.get("created_at", 0) < cutoff:
            _browser_logins.pop(state, None)


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
    auth = _psn_auth_from_settings(settings)
    refresh_expires_at = auth.get("refresh_token_expires_at") if auth else None
    return {
        "connected": bool(settings.psn_npsso or auth),
        "online_id": settings.psn_online_id,
        "refresh_token_expires_at": refresh_expires_at,
        "account_id": settings.psn_account_id,
        "connected_at": settings.psn_connected_at.isoformat() if settings.psn_connected_at else None,
        "last_sync_at": settings.psn_last_sync_at.isoformat() if settings.psn_last_sync_at else None,
        "last_sync_count": settings.psn_last_sync_count,
        "last_sync_error": settings.psn_last_sync_error,
    }


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
        client, token_response = await asyncio.to_thread(authenticate, npsso)
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
    _store_psn_auth(settings, token_response)
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


@router.delete("/disconnect")
async def psn_disconnect(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    settings.psn_npsso = None
    preferences = dict(settings.preferences or {})
    preferences.pop("psn_auth", None)
    settings.preferences = preferences
    settings.psn_online_id = None
    settings.psn_account_id = None
    settings.psn_connected_at = None
    settings.psn_last_sync_at = None
    settings.psn_last_sync_count = None
    settings.psn_last_sync_error = None
    await db.commit()
    return {"status": "disconnected"}


@router.post("/browser/start")
async def psn_browser_start(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _prune_browser_logins()
    state = secrets.token_urlsafe(32)
    _browser_logins[state] = {"user_id": current_user.id, "created_at": time.time()}
    return {
        "state": state,
        "playstation_url": "https://www.playstation.com/",
        "expires_in": 600,
    }


@router.get("/browser/status")
async def psn_browser_status(
    state: str,
    current_user: User = Depends(get_current_user),
):
    pending = _browser_logins.get(state)
    if not pending or pending.get("user_id") != current_user.id:
        raise HTTPException(status_code=404, detail="PlayStation login session not found or expired.")
    status = pending.get("status", "pending")
    if status == "completed":
        _browser_logins.pop(state, None)
        return {
            "status": "completed",
            "online_id": pending.get("online_id"),
            "account_id": pending.get("account_id"),
        }
    if status == "error":
        _browser_logins.pop(state, None)
        raise HTTPException(status_code=400, detail=pending.get("error", "PlayStation browser login failed."))
    return {"status": "pending"}


@router.post("/browser/complete")
async def psn_browser_complete(
    payload: str = Body(..., media_type="text/plain"),
    db: AsyncSession = Depends(get_db),
):
    parts = payload.strip().split("|", 2)
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid PlayStation browser login payload.")

    state, hostname, npsso = parts
    pending = _browser_logins.get(state)
    if not pending or pending.get("created_at", 0) < time.time() - 600:
        _browser_logins.pop(state, None)
        raise HTTPException(status_code=400, detail="PlayStation login session expired.")

    hostname = hostname.lower().rstrip(".")
    if not (
        hostname == "playstation.com"
        or hostname.endswith(".playstation.com")
        or hostname == "sony.com"
        or hostname.endswith(".sony.com")
    ):
        raise HTTPException(status_code=400, detail="The browser handoff must run on a PlayStation or Sony page.")

    try:
        client, token_response = await asyncio.to_thread(authenticate, npsso.strip())
        online_id = client.online_id
        account_id = client.account_id
        settings = await _get_settings(db, pending["user_id"])
        settings.psn_npsso = npsso.strip()
        _store_psn_auth(settings, token_response)
        settings.psn_online_id = online_id
        settings.psn_account_id = account_id
        settings.psn_connected_at = settings.psn_connected_at or datetime.utcnow()
        settings.psn_last_sync_error = None
        await db.commit()
        pending.update({
            "status": "completed",
            "online_id": online_id,
            "account_id": account_id,
        })
        return {"status": "connected", "online_id": online_id, "account_id": account_id}
    except Exception as exc:
        logger.warning("PSN browser authentication failed: %s", exc)
        pending.update({"status": "error", "error": f"PlayStation authentication failed: {type(exc).__name__}: {exc}"})
        raise HTTPException(status_code=400, detail=pending["error"])

@router.post("/sync")
async def psn_sync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    settings = await _get_settings(db, current_user.id)
    if not settings.psn_npsso:
        raise HTTPException(status_code=400, detail="PlayStation is not connected.")

    try:
        data = await asyncio.to_thread(fetch_library, settings.psn_npsso, _psn_auth_from_settings(settings))
    except Exception as exc:
        logger.warning("PSN sync failed for user %s: %s", current_user.id, exc)
        settings.psn_last_sync_error = f"PlayStation sync failed: {type(exc).__name__}: {exc}"
        await db.commit()
        raise HTTPException(status_code=400, detail=settings.psn_last_sync_error)

    settings.psn_online_id = data["online_id"]
    settings.psn_account_id = data["account_id"]
    if data.get("token_response"):
        _store_psn_auth(settings, data["token_response"])
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
            .where(
                GamePlatform.platform == platform,
                GamePlatform.external_id == external_id,
            )
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
                external_url=f"https://store.playstation.com/",
                metadata_json={"source": "psn"},
            )
            db.add(game_platform)

        if item.get("cover_path") and not game.cover_path:
            game.cover_path = item["cover_path"]

        stats_q = await db.execute(
            select(GameUserStats).where(
                GameUserStats.user_id == current_user.id,
                GameUserStats.game_id == game.id,
                GameUserStats.platform == platform,
            )
        )
        stats = stats_q.scalar_one_or_none()
        if not stats:
            stats = GameUserStats(
                user_id=current_user.id,
                game_id=game.id,
                platform=platform,
            )
            db.add(stats)

        for field in (
            "play_count", "playtime_minutes", "first_played_at",
            "last_played_at", "trophy_progress", "trophies_earned",
            "trophies_defined",
        ):
            if field in item:
                setattr(stats, field, item[field])
        imported += 1

    settings.psn_last_sync_at = datetime.utcnow()
    settings.psn_last_sync_count = imported
    settings.psn_last_sync_error = None
    await db.commit()
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
