"""Тесты разбиения на страницы/чанки и записи в pe_* (интеграционная часть — на реальном Postgres).

Интеграционные тесты выполняются, если задана переменная ``PE_TEST_PG`` в формате ``host:port``
(Postgres без pgvector допустим: тип ``vector`` в тестовой схеме подменяется на ``real[]``).
"""
import asyncio
import os
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

from knowledge_store import hooks, indexer, repository as repo
from knowledge_store.pages import chunk_pages, split_pages
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")
MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_knowledge_store.sql"


class PagesTests(unittest.TestCase):
    """Проверка split_pages и chunk_pages."""

    def test_markers_ocr_and_page(self):
        """Распознаются оба формата маркеров страниц."""
        t = "Страница 1 (OCR): Привет\nМир\nСтраница 2 (OCR): Вторая"
        self.assertEqual([(p.number, p.text) for p in split_pages(t)], [(1, "Привет\nМир"), (2, "Вторая")])
        t2 = "Page 1:\nA\nPage 2:\nB"
        self.assertEqual([p.number for p in split_pages(t2)], [1, 2])

    def test_pseudo_pages(self):
        """Текст без маркеров режется на псевдостраницы."""
        pages = split_pages("абзац\n" * 2000)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(p.pseudo for p in pages))
        self.assertEqual(split_pages("   "), [])

    def test_chunks_keep_page_range(self):
        """Чанки получают корректный диапазон страниц и не превышают размер."""
        pages = split_pages("Page 1:\n" + "а" * 900 + "\nPage 2:\n" + "б" * 900)
        chunks = chunk_pages(pages, max_chars=500, overlap=50)
        self.assertTrue(all(len(c.text) <= 500 for c in chunks))
        self.assertEqual(chunks[0].page_from, 1)
        self.assertEqual(chunks[-1].page_to, 2)
        self.assertTrue(any(c.page_from == 1 and c.page_to == 2 for c in chunks) or len(chunks) >= 3)


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class RepositoryIntegrationTests(unittest.TestCase):
    """Проверка SQL репозитория и хуков на реальном Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему pe_* (vector -> real[], без HNSW-индекса)."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        sql = MIGRATION.read_text(encoding="utf-8")
        sql = re.sub(r"CREATE EXTENSION[^\n]*\n", "", sql)
        sql = re.sub(r"[^\n]*USING hnsw[^\n]*\n", "", sql).replace("vector(1024)", "real[]")
        cls.conn._run("DROP TABLE IF EXISTS pe_summary_opinions, pe_facts, pe_chunks, pe_documents, pe_form_fields, pe_procurements CASCADE;")
        cls.conn._run(sql)
        # в тестовой схеме embedding — real[]; литерал pgvector '[..]' переводим в '{..}'
        cls._orig_insert = repo.SQL_INSERT_CHUNK
        repo.SQL_INSERT_CHUNK = cls._orig_insert.replace("$7::vector", "translate($7, '[]', '{}')::real[]")

    @classmethod
    def tearDownClass(cls):
        """Возвращает исходный SQL вставки чанков."""
        repo.SQL_INSERT_CHUNK = cls._orig_insert

    def q(self, sql):
        """Выполняет произвольный SELECT и возвращает stdout."""
        return self.conn._run(sql)

    def test_document_roundtrip_and_clear(self):
        """Документ пишется, повторная запись того же файла обновляет строку, clear удаляет."""
        run = asyncio.run
        run(repo.clear_expertise(self.conn, 901))
        doc = run(repo.upsert_document(self.conn, 901, "docIzvejenieFiles", "О'Нил.docx", "http://x", "abc", "текст", 1, 365))
        doc2 = run(repo.upsert_document(self.conn, 901, "docIzvejenieFiles", "О'Нил.docx", "http://x", "abc", "текст 2", 2, 365))
        self.assertEqual(doc, doc2)
        run(repo.set_document_extraction(self.conn, doc, "docIzvejenieFiles", {"status": "allow"}, {"raw_data": {"k": "значение"}}))
        self.assertEqual(self.q("SELECT text_full||'|'||pages_count||'|'||detected_type FROM pe_documents WHERE id=%d" % doc), "текст 2|2|docIzvejenieFiles")
        self.assertEqual(self.q("SELECT extraction->'raw_data'->>'k' FROM pe_documents WHERE id=%d" % doc), "значение")
        self.assertEqual(self.q("SELECT (expires_at > NOW() + interval '364 days')::text FROM pe_documents WHERE id=%d" % doc), "true")
        run(repo.clear_expertise(self.conn, 901))
        self.assertEqual(self.q("SELECT count(*) FROM pe_documents WHERE expertise_id=901"), "0")

    def test_hooks_flow_and_flag(self):
        """Хуки пишут паспорт и документ при включённом флаге и ничего не делают при выключенном."""
        @asynccontextmanager
        async def fake_db():
            """Подставляет тестовое соединение вместо боевого пула."""
            yield self.conn

        df = pd.DataFrame([{"id": 902, "law_reference": "44-ФЗ", "procurement_method": "Конкурс", "object": 6,
                            "checkType2": 1, "organization": "Орг", "expertise_object": "Док", "expertise_details": None}])
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as f:
            f.write(b"hello")
        try:
            with mock.patch.object(hooks, "_db", fake_db):
                with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "false"}):
                    asyncio.run(hooks.on_run_start(df))
                    self.assertIsNone(asyncio.run(hooks.on_document_text(902, "c", "a.txt", None, f.name, "t")))
                    self.assertEqual(self.q("SELECT count(*) FROM pe_procurements WHERE expertise_id=902"), "0")
                with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
                    asyncio.run(hooks.on_run_start(df))
                    doc_id = asyncio.run(hooks.on_document_text(902, "docIzvejenieFiles", "a.txt", "http://u", f.name,
                                                               "Страница 1 (OCR): раз\nСтраница 2 (OCR): два"))
                    self.assertIsNotNone(doc_id)
                    asyncio.run(hooks.on_document_extracted(doc_id, {"type_compliance": {"detected_type": "docIzvejenieFiles"}, "readability": {"status": "allow"}, "raw_data": {}}))
                    self.assertEqual(self.q("SELECT law||'|'||method||'|'||object_code||'|'||check_type2 FROM pe_procurements WHERE expertise_id=902"), "44-ФЗ|Конкурс|6|1")
                    self.assertEqual(self.q("SELECT pages_count||'|'||detected_type FROM pe_documents WHERE id=%d" % doc_id), "2|docIzvejenieFiles")
                    asyncio.run(hooks.on_run_start(df))  # повторный прогон очищает документы
                    self.assertEqual(self.q("SELECT count(*) FROM pe_documents WHERE expertise_id=902"), "0")
        finally:
            os.unlink(f.name)


class FakeEmbedder:
    """Заглушка сервиса эмбеддингов; ``drop`` — сколько векторов «потерять» (как EmbeddingClient при сбое пакета)."""

    def __init__(self, drop: int = 0):
        """Запоминает, сколько последних векторов не возвращать."""
        self.drop = drop

    async def get_embeddings(self, texts):
        """Возвращает по вектору длиной 1024 на каждый текст (кроме ``drop`` последних)."""
        return [[0.001 * (i + 1)] * repo.EMBEDDING_DIM for i, _ in enumerate(texts)][:len(texts) - self.drop]


class VectorTests(unittest.TestCase):
    """Проверка литерала вектора."""

    def test_vector_literal(self):
        """Литерал имеет формат pgvector, неверная размерность отвергается."""
        lit = repo.vector_literal([0.5] * repo.EMBEDDING_DIM)
        self.assertTrue(lit.startswith("[0.5,") and lit.endswith("]"))
        with self.assertRaises(ValueError):
            repo.vector_literal([0.1, 0.2])


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class IndexingIntegrationTests(unittest.TestCase):
    """Индексация чанков и очистка по сроку хранения на реальном Postgres."""

    def setUp(self):
        """Подготавливает схему (через RepositoryIntegrationTests) и подменяет соединение индексатора."""
        RepositoryIntegrationTests.setUpClass()
        self.conn = RepositoryIntegrationTests.conn

        @asynccontextmanager
        async def fake_db():
            """Подставляет тестовое соединение вместо боевого пула."""
            yield self.conn
        self.patch = mock.patch.object(indexer, "_db", fake_db)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(RepositoryIntegrationTests.tearDownClass)

    def q(self, sql):
        """Выполняет SQL и возвращает stdout."""
        return self.conn._run(sql)

    def _doc(self, eid, sha, text):
        """Создаёт документ с заданным текстом."""
        return asyncio.run(repo.upsert_document(self.conn, eid, "docIzvejenieFiles", "f.docx", None, sha, text, 2, 365))

    def test_index_and_idempotent(self):
        """Чанки записываются с диапазоном страниц; повторный запуск ничего не делает."""
        asyncio.run(repo.clear_expertise(self.conn, 910))
        doc = self._doc(910, "s1", "Страница 1 (OCR): " + "а" * 1800 + "\nСтраница 2 (OCR): " + "б" * 1800)
        st = asyncio.run(indexer.index_expertise(910, FakeEmbedder()))
        self.assertEqual(st["failed"], 0)
        self.assertGreaterEqual(st["chunks"], 3)
        self.assertEqual(self.q("SELECT count(*) FROM pe_chunks WHERE document_id=%d" % doc), str(st["chunks"]))
        self.assertEqual(self.q("SELECT min(page_from)||'-'||max(page_to) FROM pe_chunks WHERE document_id=%d" % doc), "1-2")
        self.assertEqual(self.q("SELECT array_length(embedding,1) FROM pe_chunks WHERE document_id=%d LIMIT 1" % doc), "1024")
        self.assertEqual(asyncio.run(indexer.index_expertise(910, FakeEmbedder()))["documents"], 0)

    def test_partial_embeddings_not_saved(self):
        """Если эмбеддингов вернулось меньше, чем чанков, документ не сохраняется (failed=1)."""
        asyncio.run(repo.clear_expertise(self.conn, 911))
        doc = self._doc(911, "s2", "Страница 1 (OCR): " + "в" * 3000)
        st = asyncio.run(indexer.index_expertise(911, FakeEmbedder(drop=1)))
        self.assertEqual((st["documents"], st["failed"]), (0, 1))
        self.assertEqual(self.q("SELECT count(*) FROM pe_chunks WHERE document_id=%d" % doc), "0")

    def test_purge_respects_expiry_and_legal_hold(self):
        """Очистка убирает текст и чанки просроченных документов, но не трогает legal_hold и свежие."""
        asyncio.run(repo.clear_expertise(self.conn, 912))
        old, held, fresh = self._doc(912, "o", "старый"), self._doc(912, "h", "удержан"), self._doc(912, "f", "свежий")
        asyncio.run(repo.replace_chunks(self.conn, old, 912, [type("C", (), dict(idx=0, page_from=1, page_to=1, text="t"))()], [[0.1] * 1024]))
        self.q("UPDATE pe_documents SET expires_at = NOW() - interval '1 day' WHERE id IN (%d, %d)" % (old, held))
        self.q("UPDATE pe_documents SET legal_hold = TRUE WHERE id = %d" % held)
        self.assertGreaterEqual(asyncio.run(indexer.purge_expired_texts()), 1)
        self.assertEqual(self.q("SELECT (text_full IS NULL)::text||'|'||(text_purged_at IS NOT NULL)::text FROM pe_documents WHERE id=%d" % old), "true|true")
        self.assertEqual(self.q("SELECT count(*) FROM pe_chunks WHERE document_id=%d" % old), "0")
        self.assertEqual(self.q("SELECT text_full FROM pe_documents WHERE id=%d" % held), "удержан")
        self.assertEqual(self.q("SELECT text_full FROM pe_documents WHERE id=%d" % fresh), "свежий")


if __name__ == "__main__":
    unittest.main()
