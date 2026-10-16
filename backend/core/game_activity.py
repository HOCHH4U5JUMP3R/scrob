from core.game_platforms import normalize_game_platform


def build_activity_adjustments(activities) -> dict:
    """Calculate user activity deltas while preserving provider cumulative baselines."""
    adjustments = {}
    for activity in activities:
        platform = normalize_game_platform(activity.platform)
        key = (activity.game_id, platform)
        entry = adjustments.setdefault(key, {
            "playtime_delta": 0, "manual_activity_count": 0,
            "first_played_at": None, "last_played_at": None,
        })
        duration = max(0, int(activity.duration_minutes or 0))
        if activity.source == "manual":
            entry["playtime_delta"] += duration
            entry["manual_activity_count"] += 1
        elif activity.playtime_before_minutes is not None and activity.playtime_after_minutes is not None:
            original_delta = max(0, int(activity.playtime_after_minutes) - int(activity.playtime_before_minutes))
            entry["playtime_delta"] += duration - original_delta
        if activity.played_at:
            if entry["first_played_at"] is None or activity.played_at < entry["first_played_at"]:
                entry["first_played_at"] = activity.played_at
            if entry["last_played_at"] is None or activity.played_at > entry["last_played_at"]:
                entry["last_played_at"] = activity.played_at
    return adjustments
