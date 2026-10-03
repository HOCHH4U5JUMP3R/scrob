"""Xbox Live client helpers for Scrob."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from httpx import HTTPStatusError, URL
from xbox.webapi.api.client import XboxLiveClient
from xbox.webapi.api.provider.titlehub.models import TitleFields
from xbox.webapi.api.provider.userstats.models import GeneralStatsField
from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.signed_session import SignedSession


def _oauth_config() -> tuple[str, str, str]:
    client_id = os.getenv("SCROB_XBOX_CLIENT_ID", "").strip()
    client_secret = os.getenv("SCROB_XBOX_CLIENT_SECRET", "")
    redirect_uri = os.getenv("SCROB_XBOX_REDIRECT_URI", "").strip()
    if not client_id or not redirect_uri:
        raise RuntimeError(
            "Xbox integration is not configured. Set SCROB_XBOX_CLIENT_ID "
            "and SCROB_XBOX_REDIRECT_URI."
        )
    return client_id, client_secret, redirect_uri


def authorization_url(state: str) -> str:
    client_id, client_secret, redirect_uri = _oauth_config()
    # client_secret is intentionally unused here; it must never be exposed to the browser.
    _ = client_secret
    from xbox.webapi.common.signed_session import SignedSession

    # AuthenticationManager only needs the session to construct the URL.
    # The session is closed immediately; token exchange creates its own session.
    session = SignedSession()
    try:
        manager = AuthenticationManager(session, client_id, client_secret, redirect_uri)
        return manager.generate_authorization_url(state=state)
    finally:
        # SignedSession is async; the object is only used for URL generation.
        # It does not open a network connection here.
        pass


async def exchange_code(code: str) -> OAuth2TokenResponse:
    client_id, client_secret, redirect_uri = _oauth_config()
    async with SignedSession() as session:
        manager = AuthenticationManager(session, client_id, client_secret, redirect_uri)
        await manager.request_tokens(code)
        return manager.oauth


async def build_client(oauth_json: str) -> tuple[SignedSession, AuthenticationManager, XboxLiveClient]:
    client_id, client_secret, redirect_uri = _oauth_config()
    session = SignedSession()
    manager = AuthenticationManager(session, client_id, client_secret, redirect_uri)
    manager.oauth = OAuth2TokenResponse.model_validate_json(oauth_json)
    try:
        await manager.refresh_tokens()
    except HTTPStatusError:
        await session.aclose()
        raise
    return session, manager, XboxLiveClient(manager)


async def fetch_library(oauth_json: str) -> dict[str, Any]:
    session, manager, client = await build_client(oauth_json)
    try:
        xuid = client.xuid
        gamertag = client.xsts_token.gamertag

        response = await client.titlehub.get_title_history(
            xuid,
            fields=[
                TitleFields.ACHIEVEMENT,
                TitleFields.IMAGE,
                TitleFields.SERVICE_CONFIG_ID,
                TitleFields.DETAIL,
            ],
            max_items=1000,
        )

        games: list[dict[str, Any]] = []
        for title in response.titles:
            if not title.name:
                continue

            platform = _platform_slug(title.devices, title.type)
            last_played = (
                title.title_history.last_time_played
                if title.title_history and title.title_history.last_time_played
                else None
            )
            achievement = title.achievement
            item: dict[str, Any] = {
                "title_id": title.title_id,
                "title": title.name,
                "cover_path": title.display_image,
                "platform_name": _platform_name(platform),
                "platform": platform,
                "service_config_id": title.service_config_id,
                "last_played_at": _naive_utc(last_played),
                "trophy_progress": round(achievement.progress_percentage)
                if achievement
                else None,
                "trophies_earned": (
                    {
                        "gamerscore": achievement.current_gamerscore,
                        "achievements": achievement.current_achievements,
                    }
                    if achievement
                    else None
                ),
                "trophies_defined": (
                    {
                        "gamerscore": achievement.total_gamerscore,
                        "achievements": achievement.total_achievements,
                    }
                    if achievement
                    else None
                ),
            }

            # MinutesPlayed is useful when the title exposes a service config.
            # A failure for one title must not abort the complete import.
            if title.service_config_id:
                try:
                    stats = await client.userstats.get_stats(
                        xuid,
                        title.service_config_id,
                        stats_fields=[GeneralStatsField.MINUTES_PLAYED],
                    )
                    minutes = _extract_minutes(stats)
                    if minutes is not None:
                        item["playtime_minutes"] = minutes
                except Exception:
                    pass

            games.append(item)

        if not games:
            raise RuntimeError("Xbox returned no game history")

        return {
            "xuid": xuid,
            "gamertag": gamertag,
            "games": games,
            "oauth_json": manager.oauth.model_dump_json(),
        }
    finally:
        await session.aclose()


def _extract_minutes(response: Any) -> int | None:
    for collection in getattr(response, "statlistscollection", []) or []:
        for stat in getattr(collection, "stats", []) or []:
            if str(getattr(stat, "name", "")).lower() == "minutesplayed":
                try:
                    return int(float(stat.value))
                except (TypeError, ValueError):
                    return None
    for group in getattr(response, "groups", []) or []:
        for collection in getattr(group, "statlistscollection", []) or []:
            for stat in getattr(collection, "stats", []) or []:
                if str(getattr(stat, "name", "")).lower() == "minutesplayed":
                    try:
                        return int(float(stat.value))
                    except (TypeError, ValueError):
                        return None
    return None


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _platform_slug(devices: list[str], title_type: str | None) -> str:
    values = " ".join(str(v).lower() for v in devices or [])
    title_type = (title_type or "").lower()
    if "windows" in values or "pc" in values:
        return "xbox-pc"
    if "360" in values or "xbox360" in values or "xbox 360" in title_type:
        return "xbox-360"
    if "scarlett" in values or "xbox series" in values:
        return "xbox-series"
    if "xboxone" in values or "xbox one" in values:
        return "xbox-one"
    return "xbox"


def _platform_name(platform: str) -> str:
    return {
        "xbox-360": "Xbox 360",
        "xbox-one": "Xbox One",
        "xbox-series": "Xbox Series X|S",
        "xbox-pc": "Xbox PC",
        "xbox": "Xbox",
    }[platform]
