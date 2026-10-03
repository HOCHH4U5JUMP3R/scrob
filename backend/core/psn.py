"""PlayStation Network client helpers.

PSN does not expose an official public game-library API. This module wraps
PSNAWP, an unofficial reverse-engineered client, behind a small Scrob-specific
interface so the rest of the application does not depend on its object model.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


def _dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _trophy_set(value) -> dict[str, int]:
    return {
        "bronze": int(getattr(value, "bronze", 0) or 0),
        "silver": int(getattr(value, "silver", 0) or 0),
        "gold": int(getattr(value, "gold", 0) or 0),
        "platinum": int(getattr(value, "platinum", 0) or 0),
    }


def fetch_library(npsso: str) -> dict:
    try:
        from psnawp_api import PSNAWP
    except ImportError as exc:
        raise RuntimeError("PSNAWP is not installed") from exc

    psn = PSNAWP(npsso)
    client = psn.me()

    online_id = client.online_id
    account_id = client.account_id

    # title_stats supplies PS4/PS5 playtime and last-played information.
    # Keep this independent from trophy_titles: a temporary trophy API failure
    # should not prevent the game library itself from being imported.
    stats = {}
    stats_error = None
    try:
        for title in client.title_stats():
            if not title.title_id:
                continue
            duration = title.play_duration.total_seconds() if title.play_duration else None
            stats[title.title_id] = {
                "title_id": title.title_id,
                "title": title.name,
                "cover_path": title.image_url,
                "platform_name": {
                    "ps4_game": "PlayStation 4",
                    "ps5_native_game": "PlayStation 5",
                }.get(getattr(title.category, "value", None), "PlayStation"),
                "play_count": title.play_count,
                "playtime_minutes": round(duration / 60) if duration is not None else None,
                "first_played_at": _dt(title.first_played_date_time),
                "last_played_at": _dt(title.last_played_date_time),
            }
    except Exception as exc:
        stats_error = exc
        logger.warning("PSN title stats request failed: %s", exc)

    # trophy_titles also exposes PS3/PS Vita titles which are not returned by
    # title_stats, while providing trophy progress for all supported platforms.
    trophies = {}
    trophy_error = None
    try:
        for title in client.trophy_titles(limit=None):
            title_id = title.np_title_id
            # Some trophy responses do not expose np_title_id. In that case the
            # communication id is still stable and useful as the external identity.
            external_id = title_id or title.np_communication_id
            if not external_id:
                continue

            platforms = sorted(getattr(p, "value", str(p)) for p in (title.title_platform or []))
            platform_name = ", ".join(
                {"ps3": "PlayStation 3", "ps4": "PlayStation 4", "psvita": "PlayStation Vita",
                 "ps5": "PlayStation 5", "pspc": "PlayStation PC"}.get(p.lower(), p)
                for p in platforms
            ) or "PlayStation"

            trophies[external_id] = {
                "title_id": external_id,
                "title": title.title_name,
                "cover_path": title.title_icon_url,
                "platform_name": platform_name,
                "platforms": platforms,
                "trophy_progress": title.progress,
                "trophies_earned": _trophy_set(title.earned_trophies),
                "trophies_defined": _trophy_set(title.defined_trophies),
                "last_updated_at": _dt(title.last_updated_datetime),
            }
    except Exception as exc:
        trophy_error = exc
        logger.warning("PSN trophy titles request failed: %s", exc)

    if not stats and not trophies:
        if stats_error:
            raise RuntimeError(f"PlayStation game statistics request failed: {stats_error}") from stats_error
        if trophy_error:
            raise RuntimeError(f"PlayStation trophy request failed: {trophy_error}") from trophy_error
        raise RuntimeError("PlayStation returned no game data")

    def _normalize_title(value: str | None) -> str:
        return "".join(char.lower() for char in (value or "") if char.isalnum())

    def _platform_matches(stats_item: dict, trophy_item: dict) -> bool:
        stats_platform = (stats_item.get("platform_name") or "").lower()
        trophy_platforms = {str(platform).lower() for platform in trophy_item.get("platforms", [])}
        if not trophy_platforms:
            return True
        mapping = {
            "playstation 4": "ps4",
            "playstation 5": "ps5",
            "playstation 3": "ps3",
            "playstation vita": "psvita",
            "playstation pc": "pspc",
        }
        return any(platform in trophy_platforms for name, platform in mapping.items() if name in stats_platform)

    stats_by_id = {str(item["title_id"]): item for item in stats.values()}
    unmatched_trophies = dict(trophies)

    for external_id, item in stats_by_id.items():
        trophy = trophies.get(external_id)
        if trophy is None:
            normalized_title = _normalize_title(item.get("title"))
            trophy = next(
                (
                    candidate
                    for candidate in trophies.values()
                    if _normalize_title(candidate.get("title")) == normalized_title
                    and _platform_matches(item, candidate)
                ),
                None,
            )

        if trophy is not None:
            item.update({
                "trophy_progress": trophy["trophy_progress"],
                "trophies_earned": trophy["trophies_earned"],
                "trophies_defined": trophy["trophies_defined"],
                "last_updated_at": trophy["last_updated_at"],
            })
            unmatched_trophies.pop(trophy["title_id"], None)

    # Keep trophy-only titles (including PS3/Vita) in the library.
    merged: dict[str, dict] = dict(stats_by_id)
    for external_id, item in unmatched_trophies.items():
        merged[external_id] = {
            key: value for key, value in item.items() if key != "platforms"
        }

    return {
        "online_id": online_id,
        "account_id": account_id,
        "games": list(merged.values()),
    }
