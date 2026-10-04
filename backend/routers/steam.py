import asyncio
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from urllib.parse import parse_qsl, urlparse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings as app_settings
from core.steam import authorization_url, fetch_library, get_player, verify_openid
from core.game_stats import record_game_play_activity
from db import async_sessionmaker, engine, get_db
from dependencies import get_current_user
from models.games import Game, GamePlatform, GameUserStats
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings

logger = logging.getLogger(__name__)
router = APIRouter()
_pending: dict[str, dict] = {}


class SteamCompleteRequest(BaseModel):
    redirect_url: str


class SteamAutoSyncRequest(BaseModel):
    interval: float | None = Field(default=None, ge=0.25, le=48)


async def _settings(db: AsyncSession, user_id: int) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    row = result.scalar_one_or_none()
    if row:
        return row
    row = UserSettings(user_id=user_id)
    db.add(row)
    await db.flush()
    return row


def _auth(settings: UserSettings) -> dict:
    value = (settings.preferences or {}).get("steam")
    return dict(value) if isinstance(value, dict) else {}


def _save(settings: UserSettings, auth: dict) -> None:
    preferences = dict(settings.preferences or {})
    preferences["steam"] = auth
    settings.preferences = preferences


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    auth = _auth(await _settings(db, current_user.id))
    return {
        "connected": bool(auth.get("steam_id")),
        "steam_id": auth.get("steam_id"),
        "persona_name": auth.get("persona_name"),
        "avatar": auth.get("avatar"),
        "last_sync_at": auth.get("last_sync_at"),
        "last_sync_count": auth.get("last_sync_count"),
        "last_sync_error": auth.get("last_sync_error"),
        "auto_sync_interval": auth.get("auto_sync_interval"),
    }


@router.post("/authorize")
async def authorize(current_user: User = Depends(get_current_user)):
    state = secrets.token_urlsafe(32)
    _pending[state] = {
        "user_id": current_user.id,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    callback = f"{app_settings.server_url.rstrip('/')}/api/proxy/steam/callback?state={state}"
    return {"authorization_url": authorization_url(callback)}


async def _complete_login(state: str, params: dict[str, str]) -> dict:
    pending = _pending.pop(state, None)
    if not pending or datetime.now(timezone.utc).timestamp() - pending["created_at"] > 600:
        raise HTTPException(status_code=400, detail="Steam login expired. Start again in Scrob.")

    try:
        steam_id = await verify_openid(params)
        player = await get_player(steam_id)
    except Exception as exc:
        logger.warning("Steam authentication failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Steam authentication failed: {type(exc).__name__}: {exc}") from exc

    async with async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)() as db:
        settings = await _settings(db, pending["user_id"])
        auth = _auth(settings)
        auth.update({
            "steam_id": steam_id,
            "persona_name": player.get("personaname"),
            "avatar": player.get("avatarfull") or player.get("avatarmedium"),
            "connected_at": datetime.utcnow().isoformat(),
            "last_sync_error": None,
        })
        _save(settings, auth)
        await db.commit()

    return {"status": "connected", "persona_name": player.get("personaname")}


@router.get("/callback", include_in_schema=False)
async def callback(request: Request):
    state = request.query_params.get("state", "")
    params = {k: v for k, v in request.query_params.items() if k.startswith("openid.")}
    try:
        return await _complete_login(state, params)
    except HTTPException as exc:
        return {"status": "error", "message": exc.detail}


@router.post("/complete")
async def complete(payload: SteamCompleteRequest):
    try:
        parsed = urlparse(payload.redirect_url.strip())
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        state = query.pop("state", "")
        params = {k: v for k, v in query.items() if k.startswith("openid.")}
        if not state or not params:
            raise HTTPException(status_code=400, detail="Paste the complete Steam redirect URL from the browser address bar.")
        return await _complete_login(state, params)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Steam redirect URL: {exc}") from exc


@router.post("/auto-sync")
async def auto_sync(payload: SteamAutoSyncRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    settings = await _settings(db, current_user.id)
    auth = _auth(settings)
    if payload.interval is not None and not auth.get("steam_id"):
        raise HTTPException(status_code=400, detail="Steam is not connected.")
    auth["auto_sync_interval"] = payload.interval
    _save(settings, auth)
    await db.commit()
    return {"auto_sync_interval": payload.interval}


@router.delete("/disconnect")
async def disconnect(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    settings = await _settings(db, current_user.id)
    preferences = dict(settings.preferences or {})
    preferences.pop("steam", None)
    settings.preferences = preferences
    await db.commit()
    return {"status": "disconnected"}


async def _sync_steam(db: AsyncSession, user_id: int) -> int:
    settings = await _settings(db, user_id)
    auth = _auth(settings)
    if not auth.get("steam_id"):
        raise RuntimeError("Steam is not connected.")

    data = await fetch_library(auth["steam_id"])
    imported = 0

    for item in data.get("games", []):
        appid = str(item.get("appid") or "")
        title = (item.get("name") or "Unknown Game").strip()
        if not appid:
            continue

        result = await db.execute(
            select(GamePlatform)
            .options(selectinload(GamePlatform.game))
            .where(GamePlatform.platform == "steam", GamePlatform.external_id == appid)
        )
        platform_row = result.scalar_one_or_none()

        if platform_row:
            game = platform_row.game
        else:
            result = await db.execute(select(Game).where(Game.title == title).limit(1))
            game = result.scalar_one_or_none()
            if not game:
                game = Game(title=title)
                db.add(game)
                await db.flush()

            platform_row = GamePlatform(
                game_id=game.id,
                platform="steam",
                external_id=appid,
                platform_name="Steam",
                external_url=f"https://store.steampowered.com/app/{appid}/",
                metadata_json={"source": "steam"},
            )
            db.add(platform_row)

        if not game.cover_path:
            game.cover_path = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900_2x.jpg"

        result = await db.execute(
            select(GameUserStats).where(
                GameUserStats.user_id == user_id,
                GameUserStats.game_id == game.id,
                GameUserStats.platform == "steam",
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = GameUserStats(user_id=user_id, game_id=game.id, platform="steam")
            db.add(stats)

        previous_playtime = stats.playtime_minutes
        current_playtime = int(item["playtime_forever"]) if item.get("playtime_forever") is not None else None
        last_played_at = None
        if item.get("rtime_last_played"):
            last_played_at = datetime.fromtimestamp(
                int(item["rtime_last_played"]), tz=timezone.utc
            ).replace(tzinfo=None)
        await record_game_play_activity(
            db, user_id=user_id, game_id=game.id, platform="steam",
            previous_playtime_minutes=previous_playtime,
            current_playtime_minutes=current_playtime,
            last_played_at=last_played_at, source="steam",
        )

        if item.get("playtime_forever") is not None:
            stats.playtime_minutes = int(item["playtime_forever"])
        if item.get("rtime_last_played"):
            dt = datetime.fromtimestamp(int(item["rtime_last_played"]), tz=timezone.utc).replace(tzinfo=None)
            stats.last_played_at = dt

        imported += 1

    auth["last_sync_at"] = datetime.utcnow().isoformat()
    auth["last_sync_count"] = imported
    auth["last_sync_error"] = None
    _save(settings, auth)
    await db.commit()
    return imported


async def run_steam_sync(user_id: int, job_id: int) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)() as db:
        try:
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=SyncStatus.running, current_step="Syncing Steam games")
            )
            await db.commit()
            imported = await _sync_steam(db, user_id)
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
            await db.rollback()
            settings = await _settings(db, user_id)
            auth = _auth(settings)
            auth["last_sync_error"] = f"Steam sync failed: {type(exc).__name__}: {exc}"
            _save(settings, auth)
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(
                    status=SyncStatus.failed,
                    errors=1,
                    error_message=auth["last_sync_error"],
                    current_step="Failed",
                )
            )
            await db.commit()


@router.post("/sync")
async def sync(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        imported = await _sync_steam(db, current_user.id)
    except Exception as exc:
        settings = await _settings(db, current_user.id)
        auth = _auth(settings)
        auth["last_sync_error"] = f"Steam sync failed: {type(exc).__name__}: {exc}"
        _save(settings, auth)
        await db.commit()
        raise HTTPException(status_code=400, detail=auth["last_sync_error"])

    auth = _auth(await _settings(db, current_user.id))
    return {
        "status": "synced",
        "games_imported": imported,
        "synced_at": auth.get("last_sync_at"),
    }
