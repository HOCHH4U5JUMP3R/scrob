from datetime import datetime
from types import SimpleNamespace

from core.game_activity import build_activity_adjustments


def activity(
    *,
    game_id=7,
    platform="Xbox",
    duration=30,
    source="manual",
    played_at=None,
    before=None,
    after=None,
):
    return SimpleNamespace(
        game_id=game_id,
        platform=platform,
        duration_minutes=duration,
        source=source,
        played_at=played_at,
        playtime_before_minutes=before,
        playtime_after_minutes=after,
    )


def test_manual_activity_contributes_to_display_playtime_and_count():
    played_at = datetime(2026, 10, 16, 16, 30)
    result = buildbuild_activity_adjustments([
        activity(platform="Xbox Series X", duration=45, played_at=played_at),
    ])

    entry = result[(7, "Xbox")]
    assert entry["playtime_delta"] == 45
    assert entry["manual_activity_count"] == 1
    assert entry["first_played_at"] == played_at
    assert entry["last_played_at"] == played_at


def test_editing_synced_duration_only_applies_difference_from_original_delta():
    result = build_activity_adjustments([
        activity(
            platform="PlayStation 5",
            duration=50,
            source="psn",
            played_at=datetime(2026, 10, 15, 20, 0),
            before=120,
            after=150,
        ),
    ])

    entry = result[(7, "PlayStation")]
    assert entry["playtime_delta"] == 20
    assert entry["manual_activity_count"] == 0


def test_manual_and_edited_activity_adjustments_are_combined_by_platform():
    result = build_activity_adjustments([
        activity(platform="Steam", duration=60, played_at=datetime(2026, 10, 14, 18, 0)),
        activity(
            platform="PC",
            duration=40,
            source="steam",
            played_at=datetime(2026, 10, 16, 18, 0),
            before=100,
            after=130,
        ),
    ])

    entry = result[(7, "PC")]
    assert entry["playtime_delta"] == 70
    assert entry["manual_activity_count"] == 1
    assert entry["first_played_at"] == datetime(2026, 10, 14, 18, 0)
    assert entry["last_played_at"] == datetime(2026, 10, 16, 18, 0)
