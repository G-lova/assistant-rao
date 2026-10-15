"""Тесты фундамента knowledge_store: флаг, safe_call, рендер и список миграций."""
import asyncio
import os
import unittest
from unittest import mock

from knowledge_store.safe import is_enabled, safe_call_async, safe_sync
from scripts.apply_migrations import list_migrations, render_sql


async def _ok(x):
    """Успешная корутина-заглушка."""
    return x * 2


async def _boom(x):
    """Корутина-заглушка, всегда падающая."""
    raise RuntimeError("db down")


class SafeCallTests(unittest.TestCase):
    """Проверка поведения safe_call_async / safe_sync при разных значениях флага."""

    def test_disabled_flag_does_not_call(self):
        """При выключенном флаге функция не вызывается и возвращается default."""
        with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "false"}):
            self.assertFalse(is_enabled())
            self.assertEqual(asyncio.run(safe_call_async(_ok, 2, default="d")), "d")

    def test_enabled_returns_result(self):
        """При включённом флаге возвращается результат функции."""
        with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
            self.assertEqual(asyncio.run(safe_call_async(_ok, 2)), 4)

    def test_error_is_swallowed(self):
        """Исключение не пробрасывается, возвращается default."""
        with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
            self.assertEqual(asyncio.run(safe_call_async(_boom, 1, default=-1)), -1)

    def test_sync_decorator(self):
        """Синхронный декоратор тоже глотает ошибки и учитывает флаг."""
        @safe_sync(default=0)
        def f(x):
            """Падающая функция."""
            raise ValueError(x)
        with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
            self.assertEqual(f(1), 0)


class MigrationTests(unittest.TestCase):
    """Проверка вспомогательных функций раннера миграций."""

    def test_render(self):
        """Плейсхолдер размерности подставляется."""
        self.assertIn("vector(768)", render_sql("embedding vector({{EMBEDDING_DIM}})", 768))

    def test_list_contains_knowledge_store(self):
        """Миграция хранилища знаний найдена и без неподставленных плейсхолдеров после рендера."""
        names = [p.name for p in list_migrations()]
        self.assertIn("001_knowledge_store.sql", names)
        sql = render_sql(next(p for p in list_migrations()).read_text(encoding="utf-8"), 1024)
        self.assertNotIn("{{", sql)


if __name__ == "__main__":
    unittest.main()
