from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from models.game_play_activity import GamePlayActivity


async def record_game_play_activity(
    db: AsyncSession,
    *,
    user_id: int,
    game_id: int,
    platform: str,
    previous_playtime_minutes: int | None,
    current_playtime_minutes: int | None,
    last_played_at: datetime | None,
    source: str,
) -> int:
    """Persist only newly observed cumulative playtime.

    The first observation establishes a baseline and intentionally creates no
    historical row because we cannot know when the already accumulated time
    happened. Subsequent increases become immutable activity records.
    """
    if previous_playtime_minutes is None or current_playtime_minutes is None:
        return 0

    previous = max(int(previous_playtime_minutes), 0)
    current = max(int(current_playtime_minutes), 0)

    # A provider reset/recalculation must not create negative activity.
    if current <= previous:
        return 0

    duration = current - previous
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(
        GamePlayActivity(
            user_id=user_id,
            game_id=game_id,
            platform=platform,
            played_at=last_played_at or observed_at,
            duration_minutes=duration,
            playtime_before_minutes=previous,
            playtime_after_minutes=current,
            observed_at=observed_at,
            source=source,
        )
    )
    return duration
