"""Окружение в ключах хранилища знаний: Scope, миграция 004, изоляция окружений, пул на задачу."""
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

from knowledge_store import db, repository as repo
from knowledge_store.scope import Scope, normalize_env
from scripts.apply_migrations import render_sql
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")


class ScopeTests(unittest.TestCase):
    """Нормализация окружения и запись ключа."""

    def test_normalize(self):
        """Регистр и пробелы приводятся, пустое и опасное значение отвергаются."""
        self.assertEqual(normalize_env(" Prod "), "prod")
        self.assertEqual(Scope.of("DEV", "15"), Scope("dev", 15))
        self.assertEqual(str(Scope("stage", 7)), "stage:7")
        for bad in (None, "", "prod'; drop table x", "a" * 40):
            with self.assertRaises(ValueError):
                normalize_env(bad)

    def test_render_legacy_env(self):
        """Плейсхолдер окружения подставляется и проверяется."""
        self.assertIn("'prod'", render_sql("SET env = '{{PE_LEGACY_ENV}}'", 1024, "prod"))
        with self.assertRaises(ValueError):
            render_sql("x", 1024, "prod'--")


class FakePool:
    """Пул-заглушка asyncpg: считает выданные соединения и закрытие."""

    created = 0

    def __init__(self):
        """Регистрирует создание пула."""
        FakePool.created += 1
        self.closed = False
        self.acquired = 0

    def acquire(self):
        """Возвращает контекст соединения."""
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                pool.acquired += 1
                return f"conn{pool.acquired}"

            async def __aexit__(self_inner, *exc):
                return False
        return _Ctx()

    async def close(self):
        """Закрывает пул."""
        self.closed = True


class TaskPoolTests(unittest.TestCase):
    """Пул на задачу создаётся лениво, один на задачу и закрывается в конце."""

    def setUp(self):
        """Подменяет asyncpg модулем-заглушкой."""
        FakePool.created = 0
        self.pools = []

        async def create_pool(**kw):
            pool = FakePool()
            self.pools.append(pool)
            return pool
        fake = types.SimpleNamespace(create_pool=create_pool)
        patcher = mock.patch.dict(sys.modules, {"asyncpg": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lazy_single_pool(self):
        """Без обращений пул не создаётся; несколько соединений и вложенная область — один пул."""
        async def scenario():
            async with db.task_pool():
                pass
            self.assertEqual(FakePool.created, 0)
            async with db.task_pool():
                async with db.connection() as c1:
                    pass
                async with db.task_pool():      # вложенная область использует внешний пул
                    async with db.connection() as c2:
                        pass
                results = await asyncio.gather(*(self._use() for _ in range(3)))   # дочерние задачи видят пул
            return c1, c2, results
        c1, c2, results = asyncio.run(scenario())
        self.assertEqual(FakePool.created, 1)
        self.assertTrue(self.pools[0].closed)
        self.assertEqual((c1, c2), ("conn1", "conn2"))
        self.assertEqual(len(results), 3)

    @staticmethod
    async def _use():
        """Берёт соединение из текущей области."""
        async with db.connection() as conn:
            return conn


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class EnvIsolationDbTests(unittest.TestCase):
    """Одна экспертиза в двух окружениях не пересекается."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def test_same_id_in_two_envs(self):
        """Паспорт, документы и очистка одного окружения не затрагивают другое."""
        run, c = asyncio.run, self.conn
        dev, prod = Scope("dev", 4242), Scope("prod", 4242)
        run(repo.save_procurement(c, dev, "44-ФЗ", "Конкурс", 6, 1))
        run(repo.save_procurement(c, prod, "223-ФЗ", "Аукцион", 6, 4))
        self.assertEqual(run(repo.get_procurement(c, dev))["law"], "44-ФЗ")
        self.assertEqual(run(repo.get_procurement(c, prod))["law"], "223-ФЗ")
        run(repo.upsert_document(c, dev, "docIzvejenieFiles", "a.docx", None, "same-sha", "dev text", 1, 365))
        run(repo.upsert_document(c, prod, "docIzvejenieFiles", "a.docx", None, "same-sha", "prod text", 1, 365))
        run(repo.clear_expertise(c, dev))
        self.assertEqual(c._run("SELECT count(*) FROM pe_documents WHERE expertise_id = 4242 AND env = 'dev'"), "0")
        self.assertEqual(c._run("SELECT count(*) FROM pe_documents WHERE expertise_id = 4242 AND env = 'prod'"), "1")


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class LegacyMigrationDbTests(unittest.TestCase):
    """Миграция 004 на данных, записанных до неё: строки получают окружение и новые ключи."""

    def test_migration_004_on_existing_rows(self):
        """Строки до 004 относятся к PE_LEGACY_ENV, ключи включают env."""
        host, port = PG.split(":")
        c = PsqlConn(host, int(port))
        c._run(pg_schema.DROP)
        for name in ("001_knowledge_store.sql", "002_form_fields_derived.sql"):
            c._run(pg_schema.shim((pg_schema.MIGRATIONS / name).read_text(encoding="utf-8")))
        c._run("INSERT INTO pe_procurements (expertise_id, law) VALUES (1, '44-ФЗ')")
        c._run("INSERT INTO pe_documents (expertise_id, sha256, text_full) VALUES (1, 'h', 't')")
        sql = (pg_schema.MIGRATIONS / "004_env_keys.sql").read_text(encoding="utf-8")
        c._run(pg_schema.shim(sql).replace("'dev'", "'prod'"))
        self.assertEqual(c._run("SELECT env FROM pe_procurements WHERE expertise_id = 1"), "prod")
        self.assertEqual(c._run("SELECT env FROM pe_documents WHERE expertise_id = 1"), "prod")
        c._run("INSERT INTO pe_procurements (env, expertise_id, law) VALUES ('dev', 1, '44-ФЗ')")  # тот же id, другое окружение
        with self.assertRaises(RuntimeError):
            c._run("INSERT INTO pe_procurements (env, expertise_id, law) VALUES ('dev', 1, '44-ФЗ')")
        pg_schema.build(c)  # вернуть полную схему для остальных тестов


if __name__ == "__main__":
    unittest.main()
