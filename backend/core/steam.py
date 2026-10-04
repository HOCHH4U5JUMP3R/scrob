"""Steam browser authentication and public library helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode
from xml.etree import ElementTree
import re

import httpx

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_COMMUNITY_URL = "https://steamcommunity.com"


def _parse_steam_xml(xml_text: str) -> ElementTree.Element:
    """Parse Steam's legacy XML while tolerating invalid text from game names."""
    # Steam can occasionally prefix the XML with a UTF-8 BOM. ElementTree
    # rejects a BOM when it receives an already-decoded Python string.
    xml_text = xml_text.lstrip("\ufeff")
    xml_text = re.sub(
        r"&(?!#(?:x[0-9A-Fa-f]+|[0-9]+);|[A-Za-z][A-Za-z0-9]+;)",
        "&amp;",
        xml_text,
    )
    # XML 1.0 does not allow these control characters.
    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", xml_text)
    # Steam has also returned game names containing a literal "<". Escape
    # angle brackets that are not the start of an actual XML construct.
    xml_text = re.sub(
        r"<(?!(?:/?[A-Za-z_][A-Za-z0-9_.:-]*(?:\s[^<>]*?)?/?>|![A-Z]+|\?xml\s|/?>))",
        "&lt;",
        xml_text,
    )
    return ElementTree.fromstring(xml_text)


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
    """Read the public games page.

    Steam documents this XML community feed, although it is deprecated in favour
    of the Web API. It lets Scrob use the same browser-only OpenID flow without
    asking users to create or paste an API key.
    """
    url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/games/?tab=all&xml=1"
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()

    # Steam's legacy XML feed can contain a BOM, raw ampersands, or XML
    # control characters. Keep all of the tolerant parsing in one helper.
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

        item = {
            "appid": appid,
            "name": name.strip(),
            "playtime_forever": int(float(hours or 0) * 60),
            "rtime_last_played": int(last_played) if last_played and last_played.isdigit() else None,
        }
        games.append(item)

    if not games:
        raise ValueError(
            "Steam returned no games. Make sure your Steam profile and game details are public."
        )

    return {"games": games}


def utc_from_timestamp(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
