def normalize_game_platform(value: str | None) -> str:
    name = str(value or "").strip().lower()
    if "playstation" in name or name.startswith(("ps4", "ps5", "ps3", "psn")):
        return "PlayStation"
    if "xbox" in name:
        return "Xbox"
    if "steam" in name or name in {"pc", "windows", "steam pc"}:
        return "PC"
    if "nintendo" in name or "switch" in name or "wii" in name or "3ds" in name or "2ds" in name or "gamecube" in name:
        return "Nintendo"
    return str(value or "Other").strip() or "Other"


def test_normalize_game_platform_merges_playstation_variants():
    assert normalize_game_platform("PlayStation 5") == "PlayStation"
    assert normalize_game_platform("PlayStation 4") == "PlayStation"
    assert normalize_game_platform("PSN") == "PlayStation"


def test_normalize_game_platform_merges_xbox_variants():
    assert normalize_game_platform("Xbox Series X") == "Xbox"
    assert normalize_game_platform("Xbox One") == "Xbox"


def test_normalize_game_platform_maps_steam_to_pc():
    assert normalize_game_platform("Steam") == "PC"
    assert normalize_game_platform("PC") == "PC"
