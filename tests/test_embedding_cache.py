"""Тесты кэша эмбеддингов подбора экспертов (search_experts.embedding_cache, миграция 013).

Запись и чтение кэша проверяются на настоящем Postgres (``PE_TEST_PG=host:port``).
"""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager

import numpy as np

from search_experts import embedding_cache
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")


class FakeClient:
    """Сервис эмбеддингов: вектор — [длина текста, 1, 0], запоминает отправленные тексты."""

    model = "bge-m3-test"

    def __init__(self):
        """Пустой журнал вызовов."""
        self.calls = []

    async def get_embeddings(self, texts, strict=False):
        """Возвращает векторы и запоминает тексты."""
        self.calls.append(list(texts))
        return np.array([[float(len(t)), 1.0, 0.0] for t in texts])


def run(coro):
    """Выполняет корутину."""
    return asyncio.run(coro)


class NoDbTest(unittest.TestCase):
    """Без внутренней БД — векторы считаются сервисом, повторы текста — один раз."""

    def test_fallback(self):
        """Ошибка соединения: все уникальные тексты уходят в сервис, порядок результата сохранён."""
        @asynccontextmanager
        async def broken():
            raise ConnectionError("нет БД")
            yield

        client = FakeClient()
        out = run(embedding_cache.get_embeddings(client, ["аа", "б", "аа"], connection=broken))
        self.assertEqual(client.calls, [["аа", "б"]])
        self.assertEqual(out[:, 0].tolist(), [2.0, 1.0, 2.0])


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class CacheDbTest(unittest.TestCase):
    """Кэш на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Тестовая схема (все миграции)."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def setUp(self):
        """Пустой кэш."""
        self.conn._run("TRUNCATE text_embeddings")

    def connection(self):
        """Фабрика соединений теста."""
        conn = self.conn

        @asynccontextmanager
        async def factory():
            yield conn
        return factory

    def test_second_call_from_cache(self):
        """Первый вызов считает и записывает, второй — без сервиса; новый текст — только он."""
        client = FakeClient()
        first = run(embedding_cache.get_embeddings(client, ["эксперт один", "эксперт два"], connection=self.connection()))
        second = run(embedding_cache.get_embeddings(client, ["эксперт два", "эксперт один"], connection=self.connection()))
        self.assertEqual(client.calls, [["эксперт один", "эксперт два"]])
        self.assertEqual(second.tolist(), first[::-1].tolist())
        run(embedding_cache.get_embeddings(client, ["эксперт один", "новый текст"], connection=self.connection()))
        self.assertEqual(client.calls[-1], ["новый текст"])
        self.assertEqual(int(self.conn._run("SELECT count(*) FROM text_embeddings")), 3)

    def test_model_is_part_of_key(self):
        """Другая модель — другой вектор (кэш не смешивает модели)."""
        client = FakeClient()
        run(embedding_cache.get_embeddings(client, ["текст"], connection=self.connection()))
        run(embedding_cache.get_embeddings(client, ["текст"], model="другая", connection=self.connection()))
        self.assertEqual(len(client.calls), 2)

    def test_purge(self):
        """Не использованные дольше срока удаляются."""
        run(embedding_cache.get_embeddings(FakeClient(), ["старый", "свежий"], connection=self.connection()))
        sha = embedding_cache.text_sha("старый")
        self.conn._run(f"UPDATE text_embeddings SET last_used_at = NOW() - interval '100 days' WHERE text_sha = '{sha}'")
        self.assertEqual(run(embedding_cache.purge_unused(self.conn, 90)), 1)
        self.assertEqual(int(self.conn._run("SELECT count(*) FROM text_embeddings")), 1)


if __name__ == "__main__":
    unittest.main()
