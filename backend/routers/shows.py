import asyncio
from datetime import datetime, date, timezone
from pydantic import BaseModel
from fastapi import APIRouter, BackgroundTasks, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select, update, func, case, cast as sa_cast, Text, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from models.events import WatchEvent
from models.collection import Collection, CollectionFile
from models.ratings import Rating
from models.lists import List as UserList, ListItem

from db import get_db, engine
from models.media import Media
from models.show_translation import ShowTranslation
from models.collection import Collection, CollectionFile
from models.base import CollectionSource, MediaType
from models.show import Show as ShowModel
from models.sync import SyncJob, SyncStatus
from models.users import User, UserSettings
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.rewatch import RewatchProgress
from routers.media import format_media, get_user_tmdb_key, check_tmdb_key, enrich_with_state, refresh_technical_data, _extract_show_content_rating, get_where_to_watch, _effective_sonarr, _get_global_settings, require_anon_nav_allowed

from dependencies import get_current_user, get_current_user_or_api_key, get_optional_user_or_api_key, ANON_USER_ID
from core import tmdb
from core import tvdb as tvdb_client
from core.episode_order import (
    ensure_episode_order_mapping,
    get_episode_order,
    get_mappings_for_tvdb_season,
    reconcile_divergent_episode_media,
    validate_episode_order,
)
from core.enrichment import (
    tmdb_season_covers,
    enrich_episode_from_tvdb,
    create_media_safely,
    apply_media_change_safely,
    is_unmapped_tvdb_episode,
)
from core.rewatch import (
    capped_season_episode_counts,
    total_aired_episodes,
    get_active_rewatch,
    get_active_rewatches_for_shows,
    get_rewatch_progress_media_ids,
)
from core.translations import (
    get_user_metadata_language,
    get_media_translations,
    get_show_translations,
    upsert_media_translation,
    upsert_show_translation,
    apply_media_translations,
    apply_show_translations,
)

router = APIRouter()

def _rounded_whole_star_rating(ratings: list[float | int | None]) -> float | None:
    """Average 1–10 ratings and round half-up to a whole-number rating."""
    values = [float(value) for value in ratings if value is not None]
    if not values:
        return None
    average = sum(values) / len(values)
    return float(min(10, max(1, int(average + 0.5))))




def _apply_local_artwork_overrides(show: ShowModel | None, show_data: dict) -> None:
    """Keep locally selected series and season posters in TVDB views."""
    if not show:
        return

    if str(show.poster_path or "").startswith("/media/"):
        show_data["poster_path"] = show.poster_path

    season_posters = {
        season.get("season_number"): season.get("poster_path")
        for season in (show.tmdb_data or {}).get("seasons", [])
        if isinstance(season, dict)
        and isinstance(season.get("season_number"), int)
        and str(season.get("poster_path") or "").startswith("/media/")
    }
    if season_posters:
        show_data["seasons"] = [
            {**season, "poster_path": season_posters.get(season.get("season_number"), season.get("poster_path"))}
            for season in show_data.get("seasons", [])
        ]


async def get_user_tvdb_key(db: AsyncSession, user_id: int) -> str | None:
    """Resolve the effective TVDB key (personal override, else server-wide) and
    register its subscriber PIN with the TVDB client so every downstream request
    for that key sends it on /login (#322/#325)."""
    from core import tvdb
    from models.global_settings import GlobalSettings
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    s = result.scalar_one_or_none()
    if s and s.tvdb_api_key:
        tvdb.set_subscriber_pin(s.tvdb_api_key, s.tvdb_subscriber_pin)
        return s.tvdb_api_key
    gs_result = await db.execute(select(GlobalSettings).where(GlobalSettings.id == 1))
    gs = gs_result.scalar_one_or_none()
    if gs and gs.tvdb_api_key:
        tvdb.set_subscriber_pin(gs.tvdb_api_key, gs.tvdb_subscriber_pin)
        return gs.tvdb_api_key
    return None


async def _enrich_tvdb_seasons(
    seasons: list[dict],
    mappings: list[EpisodeOrderMapping],
    *,
    tvdb_api_key: str,
    tvdb_language: str | None,
    series_tmdb_id: int | None,
    tmdb_api_key: str | None,
    metadata_language: str | None,
) -> tuple[list[dict], dict]:
    """Add translated TVDB metadata and TMDB ratings to TVDB seasons."""
    semaphore = asyncio.Semaphore(5)
    tvdb_metadata: dict[int, dict] = {}
    tmdb_show: dict = {}

    async def fetch_tvdb_season(season: dict) -> None:
        season_id = season.get("id")
        season_number = season.get("season_number")
        if season_id is None or season_number is None:
            return
        try:
            async with semaphore:
                raw = await tvdb_client.get_season(season_id, tvdb_api_key)
            tvdb_metadata[season_number] = tvdb_client.format_season(
                raw,
                language=tvdb_language,
            )
        except Exception:
            pass

    async def fetch_tmdb_show() -> None:
        nonlocal tmdb_show
        if series_tmdb_id is None or not tmdb_api_key:
            return
        try:
            tmdb_show = await tmdb.get_show(
                series_tmdb_id,
                tmdb_api_key,
                language=metadata_language,
            )
        except Exception:
            pass

    await asyncio.gather(
        *(fetch_tvdb_season(season) for season in seasons),
        fetch_tmdb_show(),
    )

    tmdb_seasons = {
        season.get("season_number"): season
        for season in (tmdb_show.get("seasons") or [])
        if season.get("season_number") is not None
    }
    mapping_counts: dict[int, dict[int, int]] = {}
    for mapping in mappings:
        if (
            mapping.tvdb_season_number is None
            or mapping.tmdb_season_number is None
        ):
            continue
        counts = mapping_counts.setdefault(mapping.tvdb_season_number, {})
        counts[mapping.tmdb_season_number] = (
            counts.get(mapping.tmdb_season_number, 0) + 1
        )

    enriched = []
    for season in seasons:
        season_number = season.get("season_number")
        tvdb_season = tvdb_metadata.get(season_number, {})
        candidates = mapping_counts.get(season_number, {})
        tmdb_season_number = (
            max(
                candidates,
                key=lambda candidate: (
                    candidates[candidate],
                    candidate == season_number,
                    -abs(candidate - season_number),
                ),
            )
            if candidates
            else season_number
        )
        tmdb_season = tmdb_seasons.get(tmdb_season_number, {})
        enriched.append({
            **season,
            "name": tvdb_season.get("name") or season.get("name"),
            "overview": (
                tvdb_season.get("overview")
                or tmdb_season.get("overview")
                or season.get("overview")
            ),
            "poster_path": (
                tvdb_season.get("poster_path")
                or season.get("poster_path")
            ),
            "air_date": (
                tvdb_season.get("air_date")
                or season.get("air_date")
                or tmdb_season.get("air_date")
            ),
            "tmdb_rating": tmdb_season.get("vote_average"),
        })
    return enriched, tmdb_show


def format_show(show: ShowModel) -> dict:
    return {
        "id": show.id,
        "tmdb_id": show.tmdb_id,
        "tvdb_id": show.tvdb_id,
        "type": "series",
        "title": show.title,
        "original_title": show.original_title,
        "overview": show.overview,
        "poster_path": show.poster_path,
        "backdrop_path": show.backdrop_path,
        "tmdb_rating": show.tmdb_rating,
        "status": show.status,
        "tagline": show.tagline,
        "first_air_date": show.first_air_date,
        "last_air_date": show.last_air_date,
        "genres": [g["name"] if isinstance(g, dict) else g for g in (show.tmdb_data or {}).get("genres", [])],
        "seasons_meta": (show.tmdb_data or {}).get("seasons", []),
        "original_language": (show.tmdb_data or {}).get("original_language"),
        "adult": (show.tmdb_data or {}).get("adult", False),
    }


@router.get("")
async def list_shows(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
    sort: str = Query(default="title"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    genre: list[str] = Query(default=[]),
    year: list[int] = Query(default=[]),
    status: list[str] = Query(default=[]),
    watched: list[str] = Query(default=[]),
    # in_list = in|out: membership of any of the user's lists (#255;
    # season-level list items live on the same series Media row and count too).
    in_list: str | None = Query(None),
):
    offset = (page - 1) * page_size
    lang = await get_user_metadata_language(db, current_user.id)

    # A show is in the user's collection when either the series itself is
    # collected (cloud/watchlist providers) or at least one countable episode
    # is collected (media-server libraries).
    episode_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.show_id.isnot(None),
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.season_number != 0,
            Media.episode_number.isnot(None),
        )
        .distinct()
        .subquery()
    )
    direct_series_tmdb_ids = (
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.series,
            Media.tmdb_id.isnot(None),
        )
        .distinct()
        .subquery()
    )

    base_query = select(ShowModel).where(
        or_(
            ShowModel.id.in_(select(episode_show_ids)),
            ShowModel.tmdb_id.in_(select(direct_series_tmdb_ids)),
        )
    )
    if in_list in ("in", "out"):
        series_in_list = (
            select(ListItem.id)
            .join(UserList, UserList.id == ListItem.list_id)
            .join(Media, Media.id == ListItem.media_id)
            .where(
                UserList.user_id == current_user.id,
                Media.media_type == MediaType.series,
                Media.tmdb_id == ShowModel.tmdb_id,
            )
            .exists()
        )
        base_query = base_query.where(series_in_list if in_list == "in" else ~series_in_list)
    if genre:
        base_query = base_query.where(or_(*[
            sa_cast(ShowModel.tmdb_data["genres"], Text).contains(f'"{g}"') for g in genre
        ]))
    if year:
        base_query = base_query.where(or_(*[ShowModel.first_air_date.like(f'{y}%') for y in year]))
    if status:
        base_query = base_query.where(ShowModel.status.in_(status))
    watched_states = set(watched)
    if watched_states and watched_states != {"watched", "unwatched"}:
        # "Watched" here means the user has watched at least one episode
        # (started); "unwatched" means zero episodes watched (not started).
        # This is deliberately not "fully watched" — that needs per-show aired
        # counts from TMDB, which enrich_with_state only computes for the
        # current page, not the full filtered/paginated set.
        watched_show_ids = (
            select(Media.show_id)
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(
                WatchEvent.user_id == current_user.id,
                WatchEvent.completed == True,
                Media.media_type == MediaType.episode,
                Media.show_id.isnot(None),
                Media.season_number.isnot(None),
                Media.season_number != 0,
            )
            .distinct()
            .subquery()
        )
        if "watched" in watched_states:
            base_query = base_query.where(ShowModel.id.in_(select(watched_show_ids)))
        else:
            base_query = base_query.where(ShowModel.id.notin_(select(watched_show_ids)))

    # Count total
    count_query = select(func.count()).select_from(base_query.subquery())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()
    total_pages = (total_count + page_size - 1) // page_size

    # Sort and Paginate
    if sort == "rating":
        q = base_query.order_by(ShowModel.tmdb_rating.desc().nulls_last(), ShowModel.id.desc())
    elif sort == "release_date":
        q = base_query.order_by(ShowModel.first_air_date.desc().nulls_last(), ShowModel.id.desc())
    elif sort == "created_at":
        added_at_sq = (
            select(Media.show_id.label("show_id"), Collection.added_at.label("added_at"))
            .join(Collection, Collection.media_id == Media.id)
            .where(
                Collection.user_id == current_user.id,
                Media.show_id.isnot(None),
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.season_number != 0,
                Media.episode_number.isnot(None),
            )
            .union_all(
                select(ShowModel.id.label("show_id"), Collection.added_at.label("added_at"))
                .select_from(Media)
                .join(Collection, Collection.media_id == Media.id)
                .join(ShowModel, ShowModel.tmdb_id == Media.tmdb_id)
                .where(
                    Collection.user_id == current_user.id,
                    Media.media_type == MediaType.series,
                    Media.tmdb_id.isnot(None),
                )
            )
            .subquery()
        )
        show_added_at_sq = (
            select(added_at_sq.c.show_id, func.max(added_at_sq.c.added_at).label("max_added_at"))
            .group_by(added_at_sq.c.show_id)
            .subquery()
        )
        q = (
            base_query
            .outerjoin(show_added_at_sq, show_added_at_sq.c.show_id == ShowModel.id)
            .order_by(show_added_at_sq.c.max_added_at.desc().nulls_last(), ShowModel.id.desc())
        )
    elif sort == "last_watched":
        last_watched_sq = (
            select(Media.show_id, func.max(WatchEvent.watched_at).label("last_watched_at"))
            .join(WatchEvent, WatchEvent.media_id == Media.id)
            .where(WatchEvent.user_id == current_user.id, Media.show_id.isnot(None))
            .group_by(Media.show_id)
            .subquery()
        )
        q = (
            base_query
            .outerjoin(last_watched_sq, last_watched_sq.c.show_id == ShowModel.id)
            .order_by(last_watched_sq.c.last_watched_at.desc().nulls_last(), ShowModel.id.desc())
        )
    else:
        # Titles are translated after querying. Use that same translation for
        # the database sort so title pagination matches the rendered order.
        display_title = func.coalesce(func.nullif(ShowTranslation.title, ""), ShowModel.title)
        q = (
            base_query
            .outerjoin(
                ShowTranslation,
                and_(
                    ShowTranslation.show_id == ShowModel.id,
                    ShowTranslation.language == lang,
                ),
            )
            .order_by(func.lower(display_title).asc(), ShowModel.id.desc())
        )

    result = await db.execute(q.limit(page_size).offset(offset))
    local_shows = result.scalars().all()
    results = [format_show(s) for s in local_shows]
    await enrich_with_state(db, current_user.id, results)

    if lang:
        show_ids = [r["id"] for r in results if r.get("id")]
        translations = await get_show_translations(db, show_ids, lang)
        apply_show_translations(results, translations)
    return {
        "page": page,
        "page_size": page_size,
        "total_results": total_count,
        "total_pages": total_pages,
        "results": results,
    }


@router.get("/years")
async def list_show_years(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Distinct first-air-date years present in the user's show collection, for the year filter."""
    episode_show_ids = (
        select(Media.show_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.show_id.isnot(None),
            Media.media_type == MediaType.episode,
            Media.season_number.isnot(None),
            Media.season_number != 0,
            Media.episode_number.isnot(None),
        )
        .distinct()
        .subquery()
    )
    direct_series_tmdb_ids = (
        select(Media.tmdb_id)
        .join(Collection, Collection.media_id == Media.id)
        .where(
            Collection.user_id == current_user.id,
            Media.media_type == MediaType.series,
            Media.tmdb_id.isnot(None),
        )
        .distinct()
        .subquery()
    )
    result = await db.execute(
        select(func.substr(ShowModel.first_air_date, 1, 4))
        .where(
            or_(
                ShowModel.id.in_(select(episode_show_ids)),
                ShowModel.tmdb_id.in_(select(direct_series_tmdb_ids)),
            ),
            ShowModel.first_air_date.isnot(None),
            ShowModel.first_air_date != "",
        )
        .distinct()
    )
    years = sorted(
        {int(row[0]) for row in result.all() if row[0] and row[0].isdigit()},
        reverse=True,
    )
    return {"years": years}


class EpisodeOrderRequest(BaseModel):
    episode_order: str
    force_refresh: bool = False


async def _run_episode_order_mapping(
    user_id: int,
    job_id: int,
    series_tmdb_id: int,
    tmdb_api_key: str,
    tvdb_api_key: str,
    force_refresh: bool,
) -> None:
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Called concurrently (up to 5 in flight) from ensure_episode_order_mapping's
    # per-episode fetch loop — each call gets its own short-lived session/connection
    # so concurrent progress writes never share a single AsyncSession.
    async def report_progress(done: int, total: int) -> None:
        async with async_session() as pdb:
            await pdb.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                processed_items=done, total_items=total, updated_at=func.now(),
            ))
            await pdb.commit()

    async with async_session() as db:
        try:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(status=SyncStatus.running))
            await db.commit()

            mapping_summary = await ensure_episode_order_mapping(
                db,
                series_tmdb_id,
                tmdb_api_key,
                tvdb_api_key,
                force=force_refresh,
                on_progress=report_progress,
            )
            tvdb_id = mapping_summary["tvdb_id"]

            preference = await get_episode_order(db, user_id, series_tmdb_id)
            if preference:
                preference.episode_order = "tvdb"
                preference.tvdb_id = tvdb_id
            else:
                db.add(UserShowEpisodeOrder(
                    user_id=user_id,
                    series_tmdb_id=series_tmdb_id,
                    episode_order="tvdb",
                    tvdb_id=tvdb_id,
                ))

            show_result = await db.execute(
                select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
            )
            local_show = show_result.scalar_one_or_none()
            if local_show:
                local_show.tvdb_id = tvdb_id
                # #162: this show's TMDB<->TVDB positions are now fully known -
                # merge any episode that was previously tracked under the "wrong"
                # (TVDB-native) numbering into its canonical TMDB-numbered row.
                await reconcile_divergent_episode_media(db, local_show)

            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.completed,
                stats={
                    "episode_order": "tvdb",
                    "series_tmdb_id": series_tmdb_id,
                    "tvdb_id": tvdb_id,
                    "mapping": mapping_summary,
                    "redirect": f"/show/tvdb/{tvdb_id}",
                },
            ))
            await db.commit()
        except Exception as exc:
            await db.rollback()
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(
                status=SyncStatus.failed, error_message=str(exc)[:900],
            ))
            await db.commit()


@router.get("/episode-order/jobs/{job_id}")
async def get_episode_order_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    result = await db.execute(
        select(SyncJob).where(SyncJob.id == job_id, SyncJob.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{series_tmdb_id}/episode-order")
async def set_show_episode_order(
    series_tmdb_id: int,
    body: EpisodeOrderRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        selected_order = validate_episode_order(body.episode_order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if selected_order == "tvdb":
        # Building the TVDB mapping fans out one TMDB request per episode and can
        # take minutes for long-running shows — always run it in the background so
        # the request never risks hitting the reverse proxy's read timeout.
        tmdb_api_key, tvdb_api_key = await asyncio.gather(
            get_user_tmdb_key(db, current_user.id),
            get_user_tvdb_key(db, current_user.id),
        )
        if not check_tmdb_key(tmdb_api_key):
            raise HTTPException(status_code=400, detail="TMDB API key not configured")
        if not tvdb_api_key:
            raise HTTPException(status_code=400, detail="TVDB API key not configured")

        job = SyncJob(
            user_id=current_user.id,
            source=CollectionSource.tmdb,
            job_type="episode_order",
            status=SyncStatus.pending,
            stats={"series_tmdb_id": series_tmdb_id},
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        background_tasks.add_task(
            _run_episode_order_mapping,
            current_user.id, job.id, series_tmdb_id, tmdb_api_key, tvdb_api_key, body.force_refresh,
        )
        return {"status": "started", "job_id": job.id}

    preference = await get_episode_order(db, current_user.id, series_tmdb_id)
    if preference:
        preference.episode_order = selected_order
    else:
        db.add(UserShowEpisodeOrder(
            user_id=current_user.id,
            series_tmdb_id=series_tmdb_id,
            episode_order=selected_order,
            tvdb_id=None,
        ))

    await db.commit()
    return {
        "episode_order": selected_order,
        "series_tmdb_id": series_tmdb_id,
        "tvdb_id": None,
        "mapping": None,
        "redirect": f"/show/{series_tmdb_id}",
    }


@router.get("/{series_tmdb_id}/episode-order/job")
async def get_active_episode_order_job(
    series_tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    """Look up an in-flight TVDB mapping job for this show, if any — lets the
    page recover its progress after a refresh instead of losing track of it."""
    result = await db.execute(
        select(SyncJob)
        .where(
            SyncJob.user_id == current_user.id,
            SyncJob.job_type == "episode_order",
            SyncJob.status.in_([SyncStatus.pending, SyncStatus.running]),
            SyncJob.stats["series_tmdb_id"].astext == str(series_tmdb_id),
        )
        .order_by(SyncJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/{series_tmdb_id}")
async def get_show(
    series_tmdb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_optional_user_or_api_key),
):
    if current_user is None:
        await require_anon_nav_allowed(db)
    effective_user_id = current_user.id if current_user else ANON_USER_ID

    settings_q = await db.execute(select(UserSettings).where(UserSettings.user_id == effective_user_id))
    _show_settings = settings_q.scalar_one_or_none()
    dropped_show_ids = set(_show_settings.dropped_shows or []) if _show_settings else set()

    # 1. Try to find locally
    show_result = await db.execute(
        select(ShowModel).where(ShowModel.tmdb_id == series_tmdb_id)
    )
    show = show_result.scalar_one_or_none()
    order_preference = await get_episode_order(db, effective_user_id, series_tmdb_id)
    selected_episode_order = order_preference.episode_order if order_preference else "tmdb"

    if show:
        # Fetch local episodes
        episodes_result = await db.execute(
            select(Media)
            .where(Media.media_type == MediaType.episode)
            .where(Media.show_id == show.id)
            .order_by(Media.season_number.asc(), Media.episode_number.asc())
        )
        episodes = episodes_result.scalars().all()

        seasons_meta = {
            s["season_number"]: s for s in (show.tmdb_data or {}).get("seasons", [])
        }

        # Include episode ratings in the payload so the derived season and
        # series ratings below can be calculated from actual user ratings.
        episode_ratings_q = await db.execute(
            select(Media.season_number, Media.episode_number, func.max(Rating.rating))
            .join(Rating, Rating.media_id == Media.id)
            .where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Rating.user_id == effective_user_id,
                Rating.season_number.is_(None),
            )
            .group_by(Media.season_number, Media.episode_number)
        )
        episode_user_ratings = {
            (season_number, episode_number): rating
            for season_number, episode_number, rating in episode_ratings_q.all()
        }

        seasons: dict = {}
        for ep in episodes:
            s_num = ep.season_number or 0
            season_poster = (
                seasons_meta.get(s_num, {}).get("poster_path") or show.poster_path
            )
            ep_formatted = format_media(ep)
            ep_formatted["poster_path"] = ep.poster_path or season_poster
            ep_formatted["user_rating"] = episode_user_ratings.get(
                (ep.season_number, ep.episode_number)
            )
            seasons.setdefault(s_num, []).append(ep_formatted)

        # Fetch networks + recommendations from TMDB if key is available
        networks = []
        recommendations = []
        cast = []
        tmdb_extra: dict | None = None
        api_key = await get_user_tmdb_key(db, effective_user_id)
        metadata_lang = await get_user_metadata_language(db, effective_user_id)
        if check_tmdb_key(api_key):
            try:
                tmdb_extra = await tmdb.get_show(series_tmdb_id, api_key=api_key, language=metadata_lang)
                networks = [
                    {
                        "id": n["id"],
                        "name": n["name"],
                        "logo_path": tmdb.poster_url(n.get("logo_path"), size="w500")
                        if n.get("logo_path")
                        else None,
                        "origin_country": n.get("origin_country"),
                    }
                    for n in tmdb_extra.get("networks", [])
                ]
                recommendations = [
                    {
                        "id": None,
                        "tmdb_id": r["id"],
                        "type": "series",
                        "title": r.get("name") or r.get("title"),
                        "original_title": r.get("original_name")
                        or r.get("original_title"),
                        "overview": r.get("overview"),
                        "poster_path": tmdb.poster_url(r.get("poster_path")),
                        "backdrop_path": tmdb.poster_url(
                            r.get("backdrop_path"), size="w1280"
                        ),
                        "release_date": r.get("first_air_date")
                        or r.get("release_date"),
                        "tmdb_rating": r.get("vote_average"),
                        "adult": r.get("adult", False),
                    }
                    for r in tmdb_extra.get("recommendations", {}).get("results", [])[
                        :12
                    ]
                ]
                await enrich_with_state(db, effective_user_id, recommendations)
                cast = [
                    {
                        "tmdb_id": c.get("id"),
                        "name": c.get("name"),
                        "character": c.get("character"),
                        "profile_path": tmdb.poster_url(c.get("profile_path")),
                    }
                    for c in tmdb_extra.get("credits", {}).get("cast", [])[:12]
                ]
                if metadata_lang:
                    try:
                        await upsert_show_translation(
                            db, show.id, metadata_lang,
                            tmdb_extra.get("name"),
                            tmdb_extra.get("overview"),
                            tmdb_extra.get("tagline"),
                            tmdb_extra.get("poster_path"),
                        )
                        await db.commit()
                    except Exception:
                        pass
            except Exception:
                pass

        state_item: dict = {"tmdb_id": series_tmdb_id, "type": "series"}
        # Pass last_episode_to_air from the already-fetched tmdb_extra so enrich_with_state
        # doesn't make a redundant second TMDB call for the same data.
        if tmdb_extra and tmdb_extra.get("last_episode_to_air"):
            state_item["_last_episode_to_air"] = tmdb_extra["last_episode_to_air"]
        await enrich_with_state(db, effective_user_id, [state_item])

        # While a rewatch is active, "watched" for this show reads from that
        # rewatch's progress instead of full history - computed once here and
        # reused below for the season stats, top-level rewatch field, and
        # (via enrich_with_state) the show's own watched/watch_pct.
        active_rewatch = await get_active_rewatch(db, effective_user_id, show.id)

        # --- Per-season states ---
        season_states: dict = {}

        # Collected and watched episode counts per season in one query
        coll_a = aliased(Collection)
        if active_rewatch:
            watched_case = case((RewatchProgress.id.isnot(None), Media.episode_number), else_=None)
        else:
            watch_a = aliased(WatchEvent)
            watched_case = case((watch_a.id.isnot(None), Media.episode_number), else_=None)
        season_stats_query = (
            select(
                Media.season_number,
                func.count(func.distinct(
                    case((coll_a.id.isnot(None), Media.episode_number), else_=None)
                )).label("collected"),
                func.count(func.distinct(watched_case)).label("watched"),
            )
            .outerjoin(coll_a, and_(coll_a.media_id == Media.id, coll_a.user_id == effective_user_id))
        )
        if active_rewatch:
            season_stats_query = season_stats_query.outerjoin(
                RewatchProgress,
                and_(RewatchProgress.media_id == Media.id, RewatchProgress.rewatch_id == active_rewatch.id),
            )
        else:
            season_stats_query = season_stats_query.outerjoin(
                watch_a, and_(watch_a.media_id == Media.id, watch_a.user_id == effective_user_id)
            )
        season_stats_q = await db.execute(
            season_stats_query.where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number.isnot(None),
                Media.episode_number.isnot(None),
            )
            .group_by(Media.season_number)
        )
        coll_per_season: dict = {}
        watched_per_season: dict = {}
        for sn, collected, watched in season_stats_q.all():
            coll_per_season[sn] = collected
            watched_per_season[sn] = watched

        if selected_episode_order == "tvdb":
            # Media rows remain in canonical TMDB positions. Re-group their
            # actual identities through the TVDB mapping for the show state.
            tvdb_stats_q = (
                select(
                    EpisodeOrderMapping.tvdb_season_number,
                    func.count(func.distinct(
                        case((coll_a.id.isnot(None), Media.id), else_=None)
                    )).label("collected"),
                    func.count(func.distinct(watched_case)).label("watched"),
                )
                .join(
                    Media,
                    and_(
                        Media.show_id == show.id,
                        Media.media_type == MediaType.episode,
                        Media.season_number == EpisodeOrderMapping.tmdb_season_number,
                        Media.episode_number == EpisodeOrderMapping.tmdb_episode_number,
                    ),
                )
                .outerjoin(
                    coll_a,
                    and_(coll_a.media_id == Media.id, coll_a.user_id == effective_user_id),
                )
            )
            if active_rewatch:
                tvdb_stats_q = tvdb_stats_q.outerjoin(
                    RewatchProgress,
                    and_(
                        RewatchProgress.media_id == Media.id,
                        RewatchProgress.rewatch_id == active_rewatch.id,
                    ),
                )
            else:
                tvdb_stats_q = tvdb_stats_q.outerjoin(
                    watch_a,
                    and_(watch_a.media_id == Media.id, watch_a.user_id == effective_user_id),
                )
            tvdb_stats_q = await db.execute(
                tvdb_stats_q.where(
                    EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
                    EpisodeOrderMapping.tvdb_season_number.isnot(None),
                ).group_by(EpisodeOrderMapping.tvdb_season_number)
            )
            coll_per_season = {}
            watched_per_season = {}
            for sn, collected, watched in tvdb_stats_q.all():
                coll_per_season[sn] = collected
                watched_per_season[sn] = watched

        # Season user ratings (stored against the show's Media row with season_number)
        show_media_q = await db.execute(
            select(Media).where(Media.tmdb_id == series_tmdb_id, Media.media_type == MediaType.series)
        )
        show_media = show_media_q.scalar_one_or_none()
        if show_media and not show_media.adult and (tmdb_extra or {}).get("adult", False):
            show_media.adult = True
            await db.commit()
        season_ratings: dict = {}
        if show_media:
            ratings_q = await db.execute(
                select(Rating.season_number, func.max(Rating.rating))
                .where(
                    Rating.media_id == show_media.id,
                    Rating.user_id == effective_user_id,
                    Rating.season_number.isnot(None),
                    Rating.episode_order == ("tvdb" if selected_episode_order == "tvdb" else None),
                )
                .group_by(Rating.season_number)
            )
            season_ratings = dict(ratings_q.all())

        # Season list membership (stored against the show's Media row with season_number)
        season_list_membership: dict[int, list[int]] = {}
        if show_media:
            user_lists_q = await db.execute(select(UserList.id).where(UserList.user_id == effective_user_id))
            user_list_ids = [r[0] for r in user_lists_q.all()]
            if user_list_ids:
                season_lists_q = await db.execute(
                    select(ListItem.season_number, ListItem.list_id).where(
                        ListItem.media_id == show_media.id,
                        ListItem.season_number.isnot(None),
                        ListItem.list_id.in_(user_list_ids),
                    )
                )
                for sn_, list_id in season_lists_q.all():
                    season_list_membership.setdefault(sn_, []).append(list_id)

        # Episode counts per season from stored TMDB metadata, capped at the
        # last aired episode for shows still airing.
        season_ep_counts = capped_season_episode_counts(show, tmdb_extra)
        if selected_episode_order == "tvdb":
            tvdb_count_q = await db.execute(
                select(
                    EpisodeOrderMapping.tvdb_season_number,
                    func.count(EpisodeOrderMapping.id),
                )
                .where(
                    EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
                    EpisodeOrderMapping.tvdb_season_number.isnot(None),
                )
                .group_by(EpisodeOrderMapping.tvdb_season_number)
            )
            season_ep_counts = {sn: count for sn, count in tvdb_count_q.all()}

        # Build states for every regular season, including seasons without
        # collection/watch activity, so their ratings always reach the UI.
        metadata_season_numbers = {
            season.get("season_number")
            for season in (show.tmdb_data or {}).get("seasons", [])
            if isinstance(season, dict)
            and isinstance(season.get("season_number"), int)
            and season.get("season_number") > 0
        }
        known_season_numbers = (
            set(coll_per_season)
            | set(season_ep_counts)
            | set(season_ratings)
            | {
                int(str(key).removeprefix("season_"))
                for key in seasons
                if str(key).removeprefix("season_").isdigit()
                and int(str(key).removeprefix("season_")) > 0
            }
            | metadata_season_numbers
        )
        for sn in known_season_numbers:
            collected = coll_per_season.get(sn, 0)
            watched = watched_per_season.get(sn, 0)
            total = season_ep_counts.get(sn, 0)

            # Use distinct (season, episode) from user's collection for calculation
            # to be consistent with how total is calculated (unique episodes in season).
            season_states[sn] = {
                "in_library": collected > 0,
                "collection_pct": min(100, int((collected / total) * 100)) if total > 0 else 0,
                "watched": watched >= total if total > 0 else False,
                "watch_pct": min(100, int((watched / total) * 100)) if total > 0 else 0,
                "watch_started": watched > 0,
                "user_rating": season_ratings.get(sn),
                "in_lists": season_list_membership.get(sn, []),
            }        # Derive missing season ratings from rated episodes, then calculate
        # the series fallback from the same season values returned to the UI.
        # Explicit series and season ratings always take precedence.
        for season_key, episode_items in seasons.items():
            try:
                season_number_value = int(str(season_key).removeprefix("season_"))
            except (TypeError, ValueError):
                continue
            if season_number_value <= 0:
                continue
            season_state = season_states.setdefault(season_number_value, {})
            if season_state.get("user_rating") is None:
                season_state["user_rating"] = _rounded_whole_star_rating(
                    [episode.get("user_rating") for episode in episode_items]
                )

        rated_season_values = [
            state.get("user_rating")
            for number, state in season_states.items()
            if number > 0 and state.get("user_rating") is not None
        ]
        if state_item.get("user_rating") is None:
            state_item["user_rating"] = _rounded_whole_star_rating(rated_season_values)
