"""Тесты разбиения на страницы/чанки и записи в pe_* (интеграционная часть — на реальном Postgres).

Интеграционные тесты выполняются, если задана переменная ``PE_TEST_PG`` в формате ``host:port``
(Postgres без pgvector допустим: тип ``vector`` в тестовой схеме подменяется на ``real[]``).
"""
from tests.flags import flags
import asyncio
import hashlib
import os
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

from knowledge_store import documents, hooks, indexer, repository as repo
from knowledge_store.pages import chunk_pages, split_pages
from knowledge_store.scope import Scope
from tests import pg_schema


def S(expertise_id: int) -> Scope:
    """Ключ тестовой экспертизы в окружении ``dev``."""
    return Scope("dev", expertise_id)
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
        pg_schema.build(cls.conn)

    def q(self, sql):
        """Выполняет произвольный SELECT и возвращает stdout."""
        return self.conn._run(sql)

    def test_document_roundtrip_and_clear(self):
        """Ссылка пишется, повторная запись того же файла обновляет строку, clear удаляет ссылку, но не документ."""
        run = asyncio.run
        run(repo.clear_expertise(self.conn, S(901)))
        sha = hashlib.sha256(b"901").hexdigest()
        ai = run(documents.save_text(self.conn, sha, None, "О'Нил.docx", "текст", documents.READER_FILE, 1))
        doc = run(repo.upsert_document(self.conn, S(901), "docIzvejenieFiles", "О'Нил.docx", "http://x", sha, ai, 1, 365))
        doc2 = run(repo.upsert_document(self.conn, S(901), "docIzvejenieFiles", "О'Нил.docx", "http://x", sha, ai, 2, 365))
        self.assertEqual(doc, doc2)
        run(repo.set_document_extraction(self.conn, doc, "docIzvejenieFiles", {"status": "allow"}, {"raw_data": {"k": "значение"}}))
        brief = run(repo.get_documents_brief(self.conn, S(901)))
        self.assertEqual((brief[0]["text_full"], brief[0]["has_text"]), ("текст", True))
        self.assertEqual(self.q("SELECT pages_count||'|'||detected_type||'|'||(text_full IS NULL)::text FROM pe_documents WHERE id=%d" % doc),
                         "2|docIzvejenieFiles|true")
        self.assertEqual(self.q("SELECT extraction->'raw_data'->>'k' FROM pe_documents WHERE id=%d" % doc), "значение")
        self.assertEqual(self.q("SELECT (expires_at > NOW() + interval '364 days')::text FROM pe_documents WHERE id=%d" % doc), "true")
        run(repo.clear_expertise(self.conn, S(901)))
        self.assertEqual(self.q("SELECT count(*) FROM pe_documents WHERE expertise_id=901"), "0")
        self.assertEqual(self.q("SELECT text FROM ai_documents WHERE id=%d" % ai), "текст")

    def test_nul_characters_are_stripped(self):
        """Нулевой байт из XML/PDF не роняет запись текста, извлечения и кэша (Postgres не принимает 0x00)."""
        run = asyncio.run
        sha = hashlib.sha256(b"nul").hexdigest()
        ai = run(documents.save_text(self.conn, sha, None, "p.xml", "Протокол\x00 подачи", documents.READER_FILE, 1))
        self.assertEqual(run(documents.find_text(self.conn, sha))["text"], "Протокол подачи")
        doc = run(repo.upsert_document(self.conn, S(905), "linkDocs", "p.xml", None, sha, ai, 1, 365))
        run(repo.set_document_extraction(self.conn, doc, "x", {"issues": ["a\x00b"]}, {"raw_data": {"k\x00": "v\x00"}}))
        self.assertEqual(self.q("SELECT extraction->'raw_data'->>'k' FROM pe_documents WHERE id=%d" % doc), "v")
        run(documents.save_extraction(self.conn, ai, "v1", "m", {"raw_data": {"t": "x\x00y"}}))
        self.assertEqual(run(documents.get_extraction(self.conn, ai, "v1", "m")), {"raw_data": {"t": "xy"}})
        self.assertEqual(documents.strip_nul(("a\x00", 1, None)), ["a", 1, None])

    def test_save_text_keeps_first_good_text(self):
        """Годный текст общего документа не заменяется другим чтением; force заменяет текст того же способа."""
        run = asyncio.run
        sha = hashlib.sha256(b"keep").hexdigest()
        rm = run(documents.save_text(self.conn, sha, None, "a.pdf", "текст РМ", documents.READER_RM_PDF, 1))
        again = run(documents.save_text(self.conn, sha, None, "a.pdf", "текст экспертизы", documents.READER_FILE, 1))
        self.assertEqual(rm, again)
        self.assertEqual(run(documents.find_text(self.conn, sha))["text"], "текст РМ")
        run(documents.save_text(self.conn, sha, None, "a.pdf", "текст экспертизы", documents.READER_FILE, 1, overwrite=True))
        self.assertEqual(run(documents.find_text(self.conn, sha))["text"], "текст РМ")     # другой способ чтения — нет
        run(documents.save_text(self.conn, sha, None, "a.pdf", "новый текст РМ", documents.READER_RM_PDF, 1, overwrite=True))
        self.assertEqual(run(documents.find_text(self.conn, sha))["text"], "новый текст РМ")
        self.q("UPDATE ai_documents SET ocr_status = 'suspect' WHERE content_sha256 = '%s'" % sha)
        self.assertIsNone(run(documents.find_text(self.conn, sha)))                            # сомнительный OCR не берём
        run(documents.save_text(self.conn, sha, None, "a.pdf", "перечитан", documents.READER_FILE, 1))
        self.assertEqual(run(documents.find_text(self.conn, sha))["text"], "перечитан")

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
                with flags({"KNOWLEDGE_STORE_ENABLED": "false"}):
                    asyncio.run(hooks.on_run_start(df, "dev"))
                    self.assertIsNone(asyncio.run(hooks.on_document_text(S(902), "c", "a.txt", None, f.name, "t")))
                    self.assertEqual(self.q("SELECT count(*) FROM pe_procurements WHERE expertise_id=902"), "0")
                with flags({"KNOWLEDGE_STORE_ENABLED": "true"}):
                    asyncio.run(hooks.on_run_start(df, "dev"))
                    doc_id = asyncio.run(hooks.on_document_text(S(902), "docIzvejenieFiles", "a.txt", "http://u", f.name,
                                                               "Страница 1 (OCR): раз\nСтраница 2 (OCR): два"))
                    self.assertIsNotNone(doc_id)
                    asyncio.run(hooks.on_document_extracted(doc_id, {"type_compliance": {"detected_type": "docIzvejenieFiles"}, "readability": {"status": "allow"}, "raw_data": {}}))
                    self.assertEqual(self.q("SELECT law||'|'||method||'|'||object_code||'|'||check_type2 FROM pe_procurements WHERE expertise_id=902"), "44-ФЗ|Конкурс|6|1")
                    self.assertEqual(self.q("SELECT pages_count||'|'||detected_type FROM pe_documents WHERE id=%d" % doc_id), "2|docIzvejenieFiles")
                    self.assertIsNone(asyncio.run(hooks.find_cached_text(f.name)))       # DOC_DEDUP_ENABLED выключен
                    with flags({"DOC_DEDUP_ENABLED": "true"}):
                        cached = asyncio.run(hooks.find_cached_text(f.name))
                        self.assertEqual(cached["text"], "Страница 1 (OCR): раз\nСтраница 2 (OCR): два")
                        self.assertEqual(cached["sha256"], documents.file_sha256(f.name))
                        self.assertIsNone(asyncio.run(hooks.find_cached_text(f.name, force=True)))
                    asyncio.run(hooks.on_run_start(df, "dev"))  # повторный прогон очищает ссылки, документ остаётся
                    self.assertEqual(self.q("SELECT count(*) FROM pe_documents WHERE expertise_id=902"), "0")
                    self.assertEqual(self.q("SELECT count(*) FROM ai_documents WHERE content_sha256 = '%s'"
                                            % documents.file_sha256(f.name)), "1")
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

    def q(self, sql):
        """Выполняет SQL и возвращает stdout."""
        return self.conn._run(sql)

    def _doc(self, eid, name, text):
        """Создаёт общий документ с текстом и ссылку экспертизы на него; возвращает (pe id, ai id)."""
        sha = hashlib.sha256(f"{eid}:{name}".encode()).hexdigest()
        ai = asyncio.run(documents.save_text(self.conn, sha, None, "f.docx", text, documents.READER_FILE, 2))
        return asyncio.run(repo.upsert_document(self.conn, S(eid), "docIzvejenieFiles", "f.docx", None, sha, ai, 2, 365)), ai

    def test_index_and_idempotent(self):
        """Чанки (нарезка по умолчанию pages_1500) записываются с диапазоном страниц; повторный запуск и другая
        экспертиза не переиндексируют; PE_CHUNKER=pages_v2 строит вторую нарезку рядом."""
        asyncio.run(repo.clear_expertise(self.conn, S(910)))
        text = "Страница 1 (OCR): " + "Первое предложение. " * 90 + "\nСтраница 2 (OCR): " + "Второе предложение. " * 90
        _, ai = self._doc(910, "s1", text)
        st = asyncio.run(indexer.index_expertise(S(910), FakeEmbedder()))
        self.assertEqual(st["failed"], 0)
        self.assertGreaterEqual(st["chunks"], 2)
        where = "document_id=%d AND chunker='pages_1500'" % ai
        self.assertEqual(self.q("SELECT count(*) FROM ai_document_chunks WHERE " + where), str(st["chunks"]))
        self.assertEqual(self.q("SELECT min(page_from)||'-'||max(page_to) FROM ai_document_chunks WHERE " + where), "1-2")
        self.assertEqual(self.q("SELECT array_length(embedding,1) FROM ai_document_chunks WHERE %s LIMIT 1" % where), "1024")
        self.assertEqual(asyncio.run(indexer.index_expertise(S(910), FakeEmbedder()))["documents"], 0)
        # тот же файл в другой экспертизе и другом окружении: документ общий, индексация не нужна
        asyncio.run(repo.upsert_document(self.conn, Scope("prod", 910), "docIzvejenieFiles", "f.docx", None,
                                         hashlib.sha256(b"910:s1").hexdigest(), ai, 2, 365))
        self.assertEqual(asyncio.run(indexer.index_expertise(Scope("prod", 910), FakeEmbedder()))["documents"], 0)
        with mock.patch.object(__import__("configs.settings", fromlist=["x"]), "PE_CHUNKER", "pages_v2"):
            self.assertEqual(asyncio.run(indexer.index_expertise(S(910), FakeEmbedder()))["documents"], 1)
        self.assertEqual(self.q("SELECT count(DISTINCT chunker) FROM ai_document_chunks WHERE document_id=%d" % ai), "2")
        from knowledge_store.pages import current_chunker
        with mock.patch.object(__import__("configs.settings", fromlist=["x"]), "PE_CHUNKER", "unknown"), self.assertRaises(ValueError):
            current_chunker()

    def test_search_finds_only_own_chunks(self):
        """Поиск фактов видит только чанки своей экспертизы и текущей нарезки (PE_CHUNKER), ближайшие — первыми."""
        from knowledge_store import facts
        asyncio.run(repo.clear_expertise(self.conn, S(913)))
        _, mine = self._doc(913, "m", "Страница 1:\nСрок поставки 30 дней")
        _, other = self._doc(914, "o", "Страница 1:\nЧужая экспертиза")
        dim = repo.EMBEDDING_DIM
        near, far = [1.0] + [0.0] * (dim - 1), [0.0, 1.0] + [0.0] * (dim - 2)
        chunk = type("C", (), dict(idx=0, page_from=1, page_to=1, text="Срок поставки 30 дней", heading="5. Сроки"))()
        model = indexer.embedding_model_name()
        asyncio.run(repo.replace_chunks(self.conn, mine, "pages_1500", model, [chunk], [far]))
        asyncio.run(repo.replace_chunks(self.conn, other, "pages_1500", model, [chunk], [near]))
        chunk2 = type("C", (), dict(idx=1, page_from=1, page_to=1, text="другая нарезка", heading=None))()
        asyncio.run(repo.replace_chunks(self.conn, mine, "pages_v2", model, [chunk, chunk2], [near, near]))
        asyncio.run(repo.replace_chunks(self.conn, mine, "rm_tokens_800", model, [chunk], [near]))
        found = asyncio.run(facts.search_fragments(self.conn, S(913), near, 5))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].text, "Срок поставки 30 дней")
        self.assertIn("5. Сроки", found[0].source)
        self.assertEqual(found[0].document_id, int(self.q("SELECT id FROM pe_documents WHERE env='dev' AND expertise_id=913")))

    def test_partial_embeddings_not_saved(self):
        """Если эмбеддингов вернулось меньше, чем чанков, документ не сохраняется (failed=1)."""
        asyncio.run(repo.clear_expertise(self.conn, S(911)))
        _, ai = self._doc(911, "s2", "Страница 1 (OCR): " + "Строка текста.\n" * 400)
        st = asyncio.run(indexer.index_expertise(S(911), FakeEmbedder(drop=1)))
        self.assertEqual((st["documents"], st["failed"]), (0, 1))
        self.assertEqual(self.q("SELECT count(*) FROM ai_document_chunks WHERE document_id=%d" % ai), "0")

    def test_purge_by_live_links(self):
        """Текст общего документа очищается, когда живых ссылок нет; legal_hold, свежие и ссылки РМ его держат."""
        asyncio.run(repo.clear_expertise(self.conn, S(912)))
        (old, a_old), (held, a_held), (fresh, a_fresh) = (self._doc(912, "o", "старый"), self._doc(912, "h", "удержан"),
                                                          self._doc(912, "f", "свежий"))
        (rm_pe, a_rm) = self._doc(912, "r", "у РМ")
        chunk = type("C", (), dict(idx=0, page_from=1, page_to=1, text="t", heading=None))()
        asyncio.run(repo.replace_chunks(self.conn, a_old, "pages_v2", "m", [chunk], [[0.1] * 1024]))
        self.q("UPDATE pe_documents SET expires_at = NOW() - interval '1 day' WHERE id IN (%d, %d, %d)" % (old, held, rm_pe))
        self.q("UPDATE pe_documents SET legal_hold = TRUE WHERE id = %d" % held)
        self.q("UPDATE ai_documents SET updated_at = NOW() - interval '2 days'")
        self.q("INSERT INTO ai_file_links (env, external_file_id, document_id) VALUES ('dev', 912001, %d)" % a_rm)
        self.assertGreaterEqual(asyncio.run(indexer.purge_expired_texts()), 1)
        self.assertEqual(self.q("SELECT (text IS NULL)::text||'|'||(text_purged_at IS NOT NULL)::text FROM ai_documents WHERE id=%d" % a_old), "true|true")
        self.assertEqual(self.q("SELECT count(*) FROM ai_document_chunks WHERE document_id=%d" % a_old), "0")
        self.assertEqual(self.q("SELECT text FROM ai_documents WHERE id=%d" % a_held), "удержан")
        self.assertEqual(self.q("SELECT text FROM ai_documents WHERE id=%d" % a_fresh), "свежий")
        self.assertEqual(self.q("SELECT text FROM ai_documents WHERE id=%d" % a_rm), "у РМ")
        brief = {d["id"]: d for d in asyncio.run(repo.get_documents_brief(self.conn, S(912)))}
        self.assertFalse(brief[old]["has_text"])
        self.assertIsNotNone(brief[old]["text_purged_at"])
