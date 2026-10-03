import httpx
from urllib.parse import urlencode

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_API_URL = "https://api.steampowered.com"


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


async def get_player(steam_api_key: str, steam_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{STEAM_API_URL}/ISteamUser/GetPlayerSummaries/v0002/",
            params={"key": steam_api_key, "steamids": steam_id, "format": "json"},
        )
        response.raise_for_status()
        players = response.json().get("response", {}).get("players", [])
    if not players:
        raise ValueError("Steam account could not be read. Check the API key and profile privacy.")
    return players[0]


async def fetch_library(steam_api_key: str, steam_id: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{STEAM_API_URL}/IPlayerService/GetOwnedGames/v0001/",
            params={
                "key": steam_api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
                "format": "json",
            },
        )
        response.raise_for_status()
        return response.json().get("response", {})
