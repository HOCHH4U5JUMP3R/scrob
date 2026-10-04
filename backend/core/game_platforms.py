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
