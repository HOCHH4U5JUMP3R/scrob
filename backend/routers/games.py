from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db import get_db
from dependencies import get_current_user_or_api_key
from models.games import Game
from models.users import User

router = APIRouter()

def _game(game: Game) -> dict:
    return {
        "id": game.id, "title": game.title, "original_title": game.original_title,
        "slug": game.slug, "overview": game.overview, "cover_path": game.cover_path,
        "backdrop_path": game.backdrop_path,
        "release_date": game.release_date.isoformat() if game.release_date else None,
        "genres": game.genres or [],
        "platforms": [{"id": p.id, "platform": p.platform, "external_id": p.external_id,
                       "platform_name": p.platform_name, "external_url": p.external_url}
                      for p in game.platforms],
    }

@router.get("")
async def list_games(db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(get_current_user_or_api_key)):
    result = await db.execute(select(Game).options(selectinload(Game.platforms)).order_by(Game.title.asc()))
    return {"results": [_game(game) for game in result.scalars().unique().all()]}

@router.get("/{game_id}")
async def get_game(game_id: int, db: AsyncSession = Depends(get_db),
                   current_user: User = Depends(get_current_user_or_api_key)):
    result = await db.execute(select(Game).options(selectinload(Game.platforms)).where(Game.id == game_id))
    game = result.scalar_one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return _game(game)
