"""Steam browser authentication and public library helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from xml.etree import ElementTree
import re

import httpx

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_COMMUNITY_URL = "https://steamcommunity.com"


def _parse_steam_xml(xml_text: str) -> ElementTree.Element:
    """Parse Steam's legacy XML while tolerating invalid text from game names."""
    xml_text = xml_text.lstrip("\ufeff")
    xml_text = re.sub(
        r"&(?!#(?:x[0-9A-Fa-f]+|[0-9]+);|[A-Za-z][A-Za-z0-9]+;)",
        "&amp;",
        xml_text,
    )
    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", xml_text)
    xml_text = re.sub(
        r"<(?!(?:/?[A-Za-z_][A-Za-z0-9_.:-]*(?:\s[^<>]*?)?/?>|![A-Z]+|\?xml\s|/?>))",
        "&lt;",
        xml_text,
    )
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        from html.parser import HTMLParser

        class _SteamParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.root = ElementTree.Element("root")
                self.stack = [self.root]

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                node = ElementTree.SubElement(self.stack[-1], tag)
                self.stack.append(node)

            def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                ElementTree.SubElement(self.stack[-1], tag)

            def handle_endtag(self, tag: str) -> None:
                for index in range(len(self.stack) - 1, 0, -1):
                    if self.stack[index].tag == tag:
                        del self.stack[index:]
                        return

            def handle_data(self, data: str) -> None:
                if data.strip():
                    current = self.stack[-1]
                    current.text = (current.text or "") + data

        parser = _SteamParser()
        parser.feed(xml_text)
        return parser.root


def _parse_steam_games_html(html_text: str) -> list[dict]:
    """Extract the public game list embedded in Steam's games page."""
    marker = "var rgGames = "
    start = html_text.find(marker)
    if start < 0:
        return []

    payload = html_text[start + len(marker):]
    try:
        games, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return []

    if not isinstance(games, list):
        return []

    result: list[dict] = []
    for game in games:
        if not isinstance(game, dict):
            continue

        appid = game.get("appid")
        name = game.get("name")
        if appid is None or not name:
            continue

        playtime = game.get("playtime_forever") or 0
        try:
            playtime = int(playtime)
        except (TypeError, ValueError):
            playtime = 0

        last_played = game.get("last_played")
        try:
            last_played = int(last_played) if last_played else None
        except (TypeError, ValueError):
            last_played = None

        result.append(
            {
                "appid": str(appid),
                "name": str(name).strip(),
                "playtime_forever": playtime,
                "rtime_last_played": last_played,
            }
        )

    return result


def authorization_url(return_to: str) -> str:
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": return_to.rsplit("/", 1)[0] + "/",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID_URL}?{urlencode(params)}"


async def verify_openid(params: dict[str, str]) -> str:
    if params.get("openid.mode") != "id_res":
        raise ValueError("Invalid Steam OpenID response.")

    verification = dict(params)
    verification["openid.mode"] = "check_authentication"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(STEAM_OPENID_URL, data=verification)
        response.raise_for_status()

    if "is_valid:true" not in response.text:
        raise ValueError("Steam OpenID verification failed.")

    prefix = "https://steamcommunity.com/openid/id/"
    claimed = params.get("openid.claimed_id", "")
    if not claimed.startswith(prefix):
        raise ValueError("Steam did not return a valid SteamID.")

    steam_id = claimed[len(prefix):].strip("/")
    if not steam_id.isdigit():
        raise ValueError("Steam returned an invalid SteamID.")
    return steam_id


async def get_player(steam_id: str) -> dict:
    """Read the public Steam profile without requiring a per-user API key."""
    url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/?xml=1"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()

    root = _parse_steam_xml(response.text)
    if root.findtext("steamID64") != steam_id:
        raise ValueError("Steam profile could not be read. Make sure the profile is public.")

    return {
        "steamid64": steam_id,
        "personaname": root.findtext("steamID"),
        "avatarfull": root.findtext("avatarFull") or root.findtext("avatarMedium"),
    }


async def fetch_library(steam_id: str) -> dict:
    """Read the public Steam games page without requiring a Steam API key."""
    xml_url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/games/?tab=all&xml=1"
    html_url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/games/?tab=all"
    headers = {
        "User-Agent": "Scrob/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(timeout=90, follow_redirects=True, headers=headers) as client:
        response = await client.get(xml_url)
        response.raise_for_status()

        root = _parse_steam_xml(response.text)
        error = root.findtext("error")
        if error:
            raise ValueError(error.strip())

        games: list[dict] = []
        for node in root.findall("./games/game"):
            appid = node.findtext("appID")
            name = node.findtext("name")
            if not appid or not name:
                continue

            hours = node.findtext("hoursOnRecord")
            last_played = node.findtext("lastPlayed")

            games.append(
                {
                    "appid": appid,
                    "name": name.strip(),
                    "playtime_forever": int(float(hours or 0) * 60),
                    "rtime_last_played": (
                        int(last_played) if last_played and last_played.isdigit() else None
                    ),
                }
            )

        if games:
            return {"games": games}

        # Steam's deprecated XML feed can return an empty result even for a
        # public library. The normal games page embeds the same public data in
        # rgGames and is the more reliable fallback.
        html_response = await client.get(html_url)
        html_response.raise_for_status()
        games = _parse_steam_games_html(html_response.text)
        if games:
            return {"games": games}

    raise ValueError(
        "Steam returned no games. Make sure your Steam profile and game details are public."
    )


def utc_from_timestamp(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
