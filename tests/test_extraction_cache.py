"""Кэш результата TypeDataExtractor в общем документе и параметр force."""
from tests.flags import flags
import asyncio
import hashlib
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from knowledge_store import documents, hooks, repository as repo
from knowledge_store.scope import Scope
from tests import pg_schema
from tests.pg_psql import PsqlConn
from tests.stubs import stubbed_import

PG = os.getenv("PE_TEST_PG")
GOOD = {"type_compliance": {"detected_type": "docIzvejenieFiles", "status": "allow"},
        "readability": {"status": "allow"}, "raw_data": {"nmck": "100"}}


class VersionTests(unittest.TestCase):
    """Ключ кэша и отбор кэшируемых результатов."""

    def test_version_depends_on_context_and_text(self):
        """Тот же контекст — та же версия; тип, объект, имя, размер блока и текст меняют её."""
        base = documents.extraction_version("docIzvejenieFiles", 6, "a.pdf", 10000, "текст")
        self.assertEqual(base, documents.extraction_version("docIzvejenieFiles", 6, "a.pdf", 10000, "текст"))
        for other in (("docProjContractFiles", 6, "a.pdf", 10000, "текст"), ("docIzvejenieFiles", 7, "a.pdf", 10000, "текст"),
                      ("docIzvejenieFiles", 6, "b.pdf", 10000, "текст"), ("docIzvejenieFiles", 6, "a.pdf", 5000, "текст"),
                      ("docIzvejenieFiles", 6, "a.pdf", 10000, "другой текст")):
            self.assertNotEqual(base, documents.extraction_version(*other))
        self.assertEqual(base.split(":")[0], documents._sources_hash())

    def test_cacheable(self):
        """Запасные ответы (тип unknown, пустые данные) не кэшируются."""
        self.assertTrue(documents.cacheable_extraction(GOOD))
        self.assertFalse(documents.cacheable_extraction({"type_compliance": {"detected_type": "unknown"}, "raw_data": {"a": 1}}))
        self.assertFalse(documents.cacheable_extraction({"type_compliance": {"detected_type": "x"}, "raw_data": {}}))
        self.assertFalse(documents.cacheable_extraction("строка"))


class PipelineCacheTests(unittest.TestCase):
    """process_parse_result: попадание в кэш не вызывает модель, промах — вызывает и сохраняет."""

    def _run(self, cached):
        """Прогоняет process_parse_result с заданным ответом кэша; возвращает (экстрактор, save_extraction, результат)."""
        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as module:
            p = module.TasksPipeline.__new__(module.TasksPipeline)
            p.force, p.ks_scope = False, None
            p.file_reader = mock.MagicMock()
            p.file_reader.read_file = mock.AsyncMock(return_value="текст")
            p.type_data_extractor = mock.MagicMock(model="m")
            p.type_data_extractor.extract_data_from_document = mock.AsyncMock(return_value=dict(GOOD))
            link = mock.MagicMock(doc_code="docIzvejenieFiles", object=6, media_links=None)
            h = module.ks_hooks
            with mock.patch.object(h, "find_cached_text", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(h, "on_document_text", mock.AsyncMock(return_value=11)), \
                    mock.patch.object(h, "on_document_extracted", mock.AsyncMock()), \
                    mock.patch.object(h, "cached_extraction", mock.AsyncMock(return_value=cached)) as getter, \
                    mock.patch.object(h, "save_extraction", mock.AsyncMock()) as saver:
                result = asyncio.run(p.process_parse_result("/tmp/a.pdf", "a.pdf", link))
            self.assertEqual(getter.call_args.args[0], 11)
            self.assertEqual(getter.call_args.args[1],
                             documents.extraction_version("docIzvejenieFiles", 6, "a.pdf", 10000, "текст"))
            return p.type_data_extractor.extract_data_from_document, saver, result

    def test_hit(self):
        """Попадание: модель не вызывается, результат из кэша."""
        extractor, saver, result = self._run({"raw_data": {"cached": True}})
        extractor.assert_not_called()
        saver.assert_not_called()
        self.assertEqual(result, {"raw_data": {"cached": True}})

    def test_miss(self):
        """Промах: модель вызывается, результат сохраняется."""
        extractor, saver, result = self._run(None)
        extractor.assert_awaited_once()
        self.assertEqual(saver.call_args.args[0], 11)
        self.assertEqual(result, GOOD)


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class CacheDbTests(unittest.TestCase):
    """Хуки кэша на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def test_roundtrip_flags_and_force(self):
        """Сохранение и чтение по ссылке; без флага и при force кэш не используется; другая экспертиза видит кэш."""
        conn = self.conn

        @asynccontextmanager
        async def fake_db():
            """Тестовое соединение вместо пула."""
            yield conn

        run = asyncio.run
        sha = hashlib.sha256(b"cache").hexdigest()
        ai = run(documents.save_text(conn, sha, None, "a.pdf", "текст", documents.READER_FILE, 1))
        link1 = run(repo.upsert_document(conn, Scope("dev", 61), "docIzvejenieFiles", "a.pdf", None, sha, ai, 1, 365))
        link2 = run(repo.upsert_document(conn, Scope("prod", 62), "docIzvejenieFiles", "a.pdf", None, sha, ai, 1, 365))
        version = documents.extraction_version("docIzvejenieFiles", 6, "a.pdf", 10000, "текст")
        with mock.patch.object(hooks, "_db", fake_db):
            with flags({"KNOWLEDGE_STORE_ENABLED": "true", "DOC_EXTRACTION_CACHE_ENABLED": "false"}):
                run(hooks.save_extraction(link1, version, "m", GOOD))
                self.assertEqual(conn._run("SELECT count(*) FROM ai_document_analysis"), "0")
            with flags({"KNOWLEDGE_STORE_ENABLED": "true", "DOC_EXTRACTION_CACHE_ENABLED": "true"}):
                run(hooks.save_extraction(link1, version, "m", {"raw_data": {}}))      # запасной ответ — нет
                self.assertEqual(conn._run("SELECT count(*) FROM ai_document_analysis"), "0")
                run(hooks.save_extraction(link1, version, "m", GOOD))
                self.assertEqual(run(hooks.cached_extraction(link2, version, "m")), GOOD)
                self.assertIsNone(run(hooks.cached_extraction(link2, version, "m", force=True)))
                self.assertIsNone(run(hooks.cached_extraction(link2, version, "other-model")))
                self.assertIsNone(run(hooks.cached_extraction(None, version, "m")))
            with flags({"KNOWLEDGE_STORE_ENABLED": "true", "DOC_EXTRACTION_CACHE_ENABLED": "false"}):
                self.assertIsNone(run(hooks.cached_extraction(link2, version, "m")))


if __name__ == "__main__":
    unittest.main()
