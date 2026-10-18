"""Steam browser authentication and public library helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from xml.etree import ElementTree
import re

import httpx

from core.config import settings

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
    """Extract Steam's public game data from known embedded games payloads."""
    for marker in ("var g_rgGameData = ", "var rgGames = "):
        start = html_text.find(marker)
        if start < 0:
            continue

        payload = html_text[start + len(marker):]
        try:
            games, _ = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError:
            continue

        if isinstance(games, dict):
            games = list(games.values())
        if not isinstance(games, list):
            continue

        result: list[dict] = []
        for game in games:
            if not isinstance(game, dict):
                continue

            appid = game.get("appid") or game.get("app_id")
            name = game.get("name")
            if appid is None or not name:
                continue

            playtime = game.get("playtime_forever")
            if playtime is None:
                playtime = game.get("hoursOnRecord")
                try:
                    playtime = int(float(playtime or 0) * 60)
                except (TypeError, ValueError):
                    playtime = 0
            else:
                try:
                    playtime = int(playtime)
                except (TypeError, ValueError):
                    playtime = 0

            last_played = game.get("last_played", game.get("rtime_last_played"))
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

        if result:
            return result

    return []


def _steam_page_failure(response: httpx.Response, source: str) -> str | None:
    """Return a safe, actionable diagnosis when Steam serves a non-library page."""
    body = response.text[:50000].lower()
    if response.status_code in (401, 403):
        return f"Steam {source} returned HTTP {response.status_code}; access to the public library is denied."
    if response.status_code == 429:
        return f"Steam {source} rate-limited the request (HTTP 429). Try again later."
    if "loginform" in body or "login_form" in body or "steamcommunity.com/login" in body:
        return f"Steam {source} redirected to a login page instead of returning the public library."
    if "captcha" in body or "verify you are human" in body or "steam guard" in body:
        return f"Steam {source} returned an anti-bot or verification page, not the game library."
    if "private" in body and ("game details" in body or "profile" in body):
        return f"Steam {source} indicates that the profile or game details are private."
    return None


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
    """Read the public Steam library and report why neither endpoint yielded games."""
    xml_url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/games/?tab=all&xml=1"
    html_url = f"{STEAM_COMMUNITY_URL}/profiles/{steam_id}/games/?tab=all"
    headers = {
        "User-Agent": "Scrob/1.0",
        "X-ValveUserAgent": "panorama",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    failures: list[str] = []

    # OpenID proves account identity; it does not authorize Steam Web API calls.
    # Prefer the official API when the operator has configured an API key.
    api_key = (settings.steam_api_key or "").strip()
    if api_key:
        async with httpx.AsyncClient(timeout=45) as client:
            api_response = await client.get(
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                params={
                    "key": api_key,
                    "steamid": steam_id,
                    "include_appinfo": 1,
                    "include_played_free_games": 1,
                },
            )
            api_response.raise_for_status()
            try:
                payload = api_response.json()
            except ValueError as exc:
                raise ValueError("Steam Web API returned invalid JSON. Check STEAM_API_KEY.") from exc
            response_data = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(response_data, dict) or "games" not in response_data:
                raise ValueError(
                    "Steam Web API did not return a game list. Check STEAM_API_KEY and confirm game details are public."
                )
            api_games = []
            for game in response_data.get("games") or []:
                if not isinstance(game, dict) or not game.get("appid"):
                    continue
                api_games.append({
                    "appid": str(game["appid"]),
                    "name": str(game.get("name") or f"Steam App {game['appid']}").strip(),
                    "playtime_forever": int(game.get("playtime_forever") or 0),
                    "rtime_last_played": None,
                })
            if api_games:
                return {"games": api_games}
            if int(response_data.get("game_count") or 0) == 0:
                raise ValueError(
                    "Steam Web API returned an empty library. Confirm this account owns games "
                    "and Game details are public."
                )
            failures.append("Steam Web API returned no parseable game entries.")

    async with httpx.AsyncClient(timeout=90, follow_redirects=True, headers=headers) as client:
        response = await client.get(xml_url)
        response.raise_for_status()
        diagnosis = _steam_page_failure(response, "legacy XML endpoint")
        if diagnosis:
            failures.append(diagnosis)
        else:
            root = _parse_steam_xml(response.text)
            error = root.findtext("error")
            if error:
                failures.append(f"Steam XML endpoint reported: {error.strip()}")
            else:
                games: list[dict] = []
                for node in root.findall("./games/game"):
                    appid = node.findtext("appID")
                    name = node.findtext("name")
                    if not appid or not name:
                        continue
                    hours = node.findtext("hoursOnRecord")
                    last_played = node.findtext("lastPlayed")
                    try:
                        playtime = int(float(hours or 0) * 60)
                    except (TypeError, ValueError):
                        playtime = 0
                    games.append({
                        "appid": appid,
                        "name": name.strip(),
                        "playtime_forever": playtime,
                        "rtime_last_played": int(last_played) if last_played and last_played.isdigit() else None,
                    })
                if games:
                    return {"games": games}
                failures.append(
                    f"Steam XML endpoint returned HTTP {response.status_code} but contained no parseable games "
                    f"(content-type: {response.headers.get('content-type', 'unknown')})."
                )

        html_response = await client.get(html_url)
        html_response.raise_for_status()
        diagnosis = _steam_page_failure(html_response, "games page")
        if diagnosis:
            failures.append(diagnosis)
        else:
            games = _parse_steam_games_html(html_response.text)
            if games:
                return {"games": games}
            failures.append(
                f"Steam games page returned HTTP {html_response.status_code} but neither supported embedded "
                "payload (g_rgGameData or rgGames) contained parseable games "
                f"(content-type: {html_response.headers.get('content-type', 'unknown')})."
            )

    detail = " ".join(failures)
    if not api_key and any("login page" in failure for failure in failures):
        detail += (
            " Steam no longer serves the legacy public library to this request. "
            "Configure STEAM_API_KEY in Scrob's backend environment to use the Steam Web API; "
            "Steam OpenID login alone does not authorize library API access."
        )
    raise ValueError("Steam library import failed. " + detail)


def utc_from_timestamp(value: int | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
