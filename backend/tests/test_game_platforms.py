from core.game_platforms import normalize_game_platform


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
