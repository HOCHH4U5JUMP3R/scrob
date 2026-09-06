import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects import postgresql

from models.base import MediaType
from routers import media, shows


class _Scalars:
    def __init__(self, value):
        self.value = value

    def all(self):
        return self.value if isinstance(self.value, list) else []


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return _Scalars(self.value)


class _Session:
    def __init__(self, results):
        self.results = iter(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(next(self.results))


class LocalizedTitleSortTests(unittest.IsolatedAsyncioTestCase):
    """The title sort must use the title shown after metadata translation."""

    @staticmethod
    def _sql(statement):
        return str(statement.compile(dialect=postgresql.dialect()))

    async def test_movies_sort_by_selected_language_translation(self):
        db = _Session([0, []])
        user = SimpleNamespace(id=7)

        with patch("routers.media.get_user_metadata_language", AsyncMock(return_value="de-DE")), \
             patch("routers.media.get_media_translations", AsyncMock(return_value={})), \
             patch("routers.media.enrich_with_state", AsyncMock()):
            await media.list_media(MediaType.movie, "title", 1, 30, [], [], [], None, db, user)

        sql = self._sql(db.statements[1])
        self.assertIn("LEFT OUTER JOIN media_translations", sql)
        self.assertIn("media_translations.language = %(language_1)s", sql)
        self.assertIn("coalesce(nullif(media_translations.title", sql)

    async def test_shows_sort_by_selected_language_translation(self):
        db = _Session([0, []])
        user = SimpleNamespace(id=7)

        with patch("routers.shows.get_user_metadata_language", AsyncMock(return_value="de-DE")), \
             patch("routers.shows.get_show_translations", AsyncMock(return_value={})), \
             patch("routers.shows.enrich_with_state", AsyncMock()):
            await shows.list_shows(db, user, "title", 1, 30, [], [], [], [], None)

        sql = self._sql(db.statements[1])
        self.assertIn("LEFT OUTER JOIN show_translations", sql)
        self.assertIn("show_translations.language = %(language_1)s", sql)
        self.assertIn("coalesce(nullif(show_translations.title", sql)


if __name__ == "__main__":
    unittest.main()
