"""Общий документ по хэшу: нарезка pages_v2, перенос текстов миграцией 005, повторное использование текста в пайплайне."""
import asyncio
import csv
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from knowledge_store.pages import CHUNKER_PAGES_V2, chunk_structured, embedding_text, split_pages
from tests import pg_schema
from tests.pg_psql import PsqlConn
from tests.stubs import stubbed_import

PG = os.getenv("PE_TEST_PG")


class ChunkerTests(unittest.TestCase):
    """Нарезка pages_v2."""

    def setUp(self):
        """Документ: заголовок, два раздела, таблица на второй странице."""
        self.text = ("Страница 1:\nИЗВЕЩЕНИЕ О ПРОВЕДЕНИИ ЭЛЕКТРОННОГО КОНКУРСА\n1. Общие положения\n"
                     + "Текст пункта общих положений. " * 40
                     + "\n2. Требования к участникам\n2.1. Участник должен соответствовать требованиям. "
                     + "Ещё требование. " * 30
                     + "\nСтраница 2:\n| Наименование | Кол-во | Цена |\n"
                     + "\n".join(f"| Товар {i} | {i} | {i * 100} |" for i in range(80))
                     + "\n3. Срок поставки\nПоставка в течение 30 дней.")
        self.chunks = chunk_structured(split_pages(self.text))

    def test_sizes_pages_and_order(self):
        """Чанки не больше лимита, страницы возрастают, весь текст покрыт."""
        self.assertTrue(all(len(c.text) <= 1800 for c in self.chunks))
        self.assertEqual([c.idx for c in self.chunks], list(range(len(self.chunks))))
        self.assertEqual((self.chunks[0].page_from, self.chunks[-1].page_to), (1, 2))
        joined = "\n".join(c.text for c in self.chunks)
        for needle in ("Общие положения", "Товар 0 |", "Товар 79 |", "Поставка в течение 30 дней."):
            self.assertIn(needle, joined)

    def test_sections_start_new_chunks(self):
        """Новый раздел начинает чанк, заголовок сохраняется в heading."""
        last = self.chunks[-1]
        self.assertTrue(last.text.startswith("3. Срок поставки"))
        self.assertEqual(last.heading, "3. Срок поставки")
        self.assertEqual(self.chunks[0].heading, "ИЗВЕЩЕНИЕ О ПРОВЕДЕНИИ ЭЛЕКТРОННОГО КОНКУРСА")

    def test_table_continuation_repeats_header(self):
        """Продолжение таблицы начинается с её шапки, строки таблицы не рвутся."""
        cont = [c for c in self.chunks if c.text.startswith("| Наименование") and "Товар 79" in c.text]
        self.assertEqual(len(cont), 1)
        self.assertNotIn("| Наименование", cont[0].text.split("\n", 1)[1])
        for c in self.chunks:
            for line in c.text.split("\n"):
                if line.startswith("|"):
                    self.assertTrue(line.endswith("|"))

    def test_embedding_text_adds_heading_once(self):
        """Заголовок добавляется в текст эмбеддинга, только если чанк с него не начинается."""
        table = next(c for c in self.chunks if c.text.startswith("| Наименование") and "Товар 79" in c.text)
        self.assertTrue(embedding_text(table).startswith(table.heading + "\n"))
        self.assertEqual(embedding_text(self.chunks[-1]), self.chunks[-1].text)

    def test_long_paragraph_split_and_overlap(self):
        """Длинный абзац делится по предложениям; при продолжении раздела есть перекрытие строк."""
        long = "Страница 1:\n" + " ".join(f"Предложение номер {i}." for i in range(400))
        chunks = chunk_structured(split_pages(long), max_chars=500, overlap_chars=120)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c.text) <= 500 for c in chunks))
        self.assertTrue(any(chunks[i].text.split("\n")[0] in chunks[i - 1].text for i in range(1, len(chunks))))
        with self.assertRaises(ValueError):
            chunk_structured(split_pages(long), max_chars=100, overlap_chars=100)

    def test_constant(self):
        """Имя нарезки совпадает с частичным индексом миграции 005."""
        sql = (pg_schema.MIGRATIONS / "005_shared_documents.sql").read_text(encoding="utf-8")
        self.assertIn(f"chunker = '{CHUNKER_PAGES_V2}'", sql)


class EvalChunkersTests(unittest.TestCase):
    """Вспомогательные функции скрипта сравнения нарезок."""

    def test_rank_and_recall(self):
        """Место цитаты в выдаче и сводка recall@k/MRR."""
        from scripts import eval_chunkers as ev
        self.assertEqual(ev.rank_of_quote(["другое", "срок поставки 30 дней", "срок"], "Срок поставки 30 дней"), 2)
        self.assertIsNone(ev.rank_of_quote(["другое"], "срок поставки"))
        summary = ev.recall_summary([1, 5, None, 20])
        self.assertEqual((summary["recall@4"], summary["recall@8"], summary["recall@16"]), (0.25, 0.5, 0.5))
        self.assertEqual(summary["mrr"], round((1 + 0.2 + 0.05) / 4, 3))

    def test_coverage_and_auc(self):
        """coverage как в AIRepo и AUC разделения пар."""
        from scripts import eval_chunkers as ev
        import numpy as np
        a = np.array([[1, 0], [0, 1]], dtype=np.float32)
        self.assertEqual(ev.coverage(a, np.array([[1, 0.01]], dtype=np.float32)), (0.5, 1.0))
        self.assertEqual(ev.auc([0.9, 0.8], [0.1, 0.8]), 0.875)
        self.assertIsNone(ev.auc([], [0.1]))
        self.assertEqual(ev.parse_vector("[1,2]").tolist(), [1.0, 2.0])
        self.assertEqual(ev.parse_vector("{1,2}").tolist(), [1.0, 2.0])


class FakeConn:
    """Соединение-заглушка для диагностики: факты и тексты одной экспертизы."""

    def __init__(self, facts, texts):
        """Запоминает строки выборок."""
        self.facts, self.texts = facts, texts

    async def fetch(self, sql, *params):
        """Возвращает факты или тексты по тексту запроса."""
        return self.facts if "pe_facts" in sql else [{"id": i, "text": t} for i, t in enumerate(self.texts)]


class HashEmbedder:
    """Детерминированные эмбеддинги: мешок слов по хэшу (близкие тексты — близкие векторы)."""

    def __init__(self):
        """Считает запросы."""
        self.calls = 0

    async def get_embeddings(self, texts):
        """Вектор частот слов по 64 корзинам."""
        import numpy as np
        self.calls += 1
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            out.append(v + 1e-3)
        return out


class VariantsTests(unittest.TestCase):
    """Диагностика вариантов нарезки в памяти."""

    def test_variants_report_and_dump(self):
        """Цитата, разрезанная границей, снижает found_any; кэш не запрашивает одинаковые тексты повторно."""
        from scripts import eval_chunkers as ev
        body = "\n".join(f"{i}. Пункт номер {i}. " + "Текст пункта про поставку товара. " * 12 for i in range(1, 30))
        text = "Страница 1:\n" + body
        quote = "Пункт номер 15. Текст пункта про поставку товара."
        facts = [{"fact_key": "k1", "label": "1.1. Наличие пункта номер 15", "quote": quote}]
        emb = HashEmbedder()
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            dump = fh.name
        try:
            res = asyncio.run(ev.run_variants(FakeConn(facts, [text]), emb, "dev", [1],
                                              ["pages_1500", "v2_1200", "v2_1200_nohead"], dump))
            with open(dump, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        finally:
            os.unlink(dump)
        self.assertEqual(rows[0], ["expertise_id", "fact_key", "label", "quote", "pages_1500", "v2_1200", "v2_1200_nohead"])
        for name in ("pages_1500", "v2_1200", "v2_1200_nohead"):
            self.assertEqual(res[name]["n"], 1)
            self.assertEqual(res[name]["found_any"], 1.0)
            self.assertGreater(res[name]["chunks"], 1)
        self.assertGreater(res["embeddings_requested"], 0)
        self.assertEqual(ev.recall_summary([None, 2])["found_any"], 0.5)


class EvalSimilarityTests(unittest.TestCase):
    """Подбор порогов похожих документов, заготовка пар, переиндексация."""

    def test_rule_and_report(self):
        """Правило «почти копия» находит редакции; отчёт по порогам считает AUC."""
        from scripts import eval_chunkers as ev
        import numpy as np
        points = [(0.95, 0.97, "same"), (0.92, 0.96, "same"), (0.7, 0.9, "related"), (0.1, 0.86, "unrelated")]
        rule = ev.best_same_rule(points)
        self.assertEqual((rule["f1"], rule["precision"], rule["recall"]), (1.0, 1.0, 1.0))
        self.assertIsNone(ev.best_same_rule([(0.5, 0.9, "related")]))
        rows = [(np.array([0.99, 0.97]), "same"), (np.array([0.9, 0.5]), "related"), (np.array([0.3, 0.2]), "unrelated")]
        report = ev.similarity_report(rows)
        self.assertEqual(report["0.85"]["mean_coverage"], {"same": 1.0, "related": 0.5, "unrelated": 0.0})
        self.assertEqual(report["0.85"]["auc_linked_vs_unrelated"], 1.0)
        self.assertEqual(report["0.95"]["auc_same_vs_related"], 1.0)

    def test_pair_sample_and_read(self):
        """Разные типы — unrelated, один тип — related? (на разметку); неразмеченные строки не читаются."""
        from scripts import eval_chunkers as ev
        typed = [(1, "A"), (2, "B"), (3, "A"), (4, "A"), (5, "C"), (6, "C")]
        unrelated, related = ev.pair_sample(typed, 10, skip={(3, 4)})
        self.assertEqual([r[:3] for r in unrelated], [(1, 2, "unrelated")])
        self.assertEqual({r[:3] for r in related}, {(1, 3, "related?"), (5, 6, "related?")})
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
            fh.write("doc_a,doc_b,label,type_a,type_b\n1,2,same,,\n3,4,related?,A,A\n5,6,unrelated,C,D\n")
        try:
            self.assertEqual(ev.read_pairs(fh.name), [(1, 2, "same"), (5, 6, "unrelated")])
        finally:
            os.unlink(fh.name)

    def test_reindex_continues_after_error(self):
        """Переиндексация: сбой одной экспертизы не останавливает остальные, итог суммируется."""
        from knowledge_store import indexer
        from knowledge_store.scope import Scope
        from scripts import reindex_pages_v2 as rx

        async def fake_index(scope, embedder):
            if scope.expertise_id == 2:
                raise RuntimeError("сбой")
            return {"documents": 1, "chunks": 3, "failed": 0}
        with mock.patch.object(indexer, "build_embedder", lambda mgr: object()), \
                mock.patch.object(indexer, "index_expertise", fake_index):
            report = asyncio.run(rx.reindex([Scope("dev", 1), Scope("dev", 2), Scope("dev", 3)], object()))
        self.assertEqual(report["total"], {"documents": 2, "chunks": 6, "failed": 0})
        self.assertIn("error", report["dev:2"])


class PipelineReuseTests(unittest.TestCase):
    """process_parse_result берёт текст общего документа вместо чтения файла."""

    def _pipeline(self, module, force=False):
        """Собирает TasksPipeline без конструктора (без БД и LLM)."""
        p = module.TasksPipeline.__new__(module.TasksPipeline)
        p.force = force
        p.ks_scope = None
        p.file_reader = mock.MagicMock()
        p.file_reader.read_file = mock.AsyncMock(return_value="прочитано заново")
        p.type_data_extractor = mock.MagicMock()
        p.type_data_extractor.extract_data_from_document = mock.AsyncMock(return_value={"raw_data": {}})
        return p

    def test_cached_text_skips_reading(self):
        """При найденном тексте FileReader не вызывается, текст уходит в извлечение и в хранилище."""
        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as module:
            p = self._pipeline(module)
            link = mock.MagicMock(doc_code="docIzvejenieFiles", object=6, media_links="http://u")
            hooks = module.ks_hooks
            with mock.patch.object(hooks, "find_cached_text", mock.AsyncMock(return_value={"id": 5, "text": "из хранилища", "reader": "rm_pdf_text"})), \
                    mock.patch.object(hooks, "on_document_text", mock.AsyncMock(return_value=None)) as saved, \
                    mock.patch.object(hooks, "on_document_extracted", mock.AsyncMock()):
                asyncio.run(p.process_parse_result("/tmp/f.pdf", "f.pdf", link))
            p.file_reader.read_file.assert_not_called()
            self.assertEqual(saved.call_args.args[5], "из хранилища")
            chunks = p.type_data_extractor.extract_data_from_document.call_args.args[0]
            self.assertIn("из хранилища", chunks[0])

    def test_force_reads_file(self):
        """При force поиск готового текста не используется (find_cached_text получает force=True)."""
        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as module:
            p = self._pipeline(module, force=True)
            link = mock.MagicMock(doc_code="docIzvejenieFiles", object=6, media_links=None)
            hooks = module.ks_hooks
            with mock.patch.object(hooks, "find_cached_text", mock.AsyncMock(return_value=None)) as finder, \
                    mock.patch.object(hooks, "on_document_text", mock.AsyncMock(return_value=None)) as saved, \
                    mock.patch.object(hooks, "on_document_extracted", mock.AsyncMock()):
                asyncio.run(p.process_parse_result("/tmp/f.pdf", "f.pdf", link))
            self.assertTrue(finder.call_args.kwargs["force"])
            self.assertTrue(saved.call_args.kwargs["force"])
            p.file_reader.read_file.assert_awaited_once()


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class MakePairsDbTests(unittest.TestCase):
    """Заготовка пар по кэшу анализов РМ."""

    def test_make_pairs(self):
        """Редакции — из version_diff, типы — из doc_type; документы без текста пропускаются."""
        from scripts import eval_chunkers as ev
        host, port = PG.split(":")
        c = PsqlConn(host, int(port))
        pg_schema.build(c)
        ids = []
        for i in range(6):
            sha = hashlib.sha256(f"p{i}".encode()).hexdigest()
            text = "NULL" if i == 5 else f"'t{i}'"
            ids.append(int(c._run(f"INSERT INTO ai_documents (content_sha256, text) VALUES ('{sha}', {text}) RETURNING id").split()[0]))
        c._run(f"INSERT INTO ai_document_analysis (document_id, analysis_type, prompt_version, model, result) VALUES "
               f"({ids[1]}, 'version_diff:{ids[0]}', 'v', 'm', '{{}}'), ({ids[2]}, 'version_diff:{ids[5]}', 'v', 'm', '{{}}')")
        for i, t in enumerate(["A", "A", "B", "B", "C", "C"]):
            c._run(f"INSERT INTO ai_document_analysis (document_id, analysis_type, prompt_version, model, result) "
                   f"VALUES ({ids[i]}, 'doc_type', 'v', 'm', '{{\"detected_type\": \"{t}\"}}')")
        rows = asyncio.run(ev.make_pairs(c, 10))
        same = [r for r in rows if r[2] == "same"]
        self.assertEqual([(r[0], r[1]) for r in same], [(ids[1], ids[0])])
        self.assertTrue(all(ids[5] not in r[:2] for r in rows))
        self.assertTrue(all(r[3] != r[4] for r in rows if r[2] == "unrelated"))
        self.assertTrue(all(r[3] == r[4] for r in rows if r[2] == "related?"))


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class Migration005DbTests(unittest.TestCase):
    """Перенос текстов pe_documents в ai_documents."""

    def test_texts_moved_and_rm_text_kept(self):
        """Тексты переносятся по хэшу, текст РМ не заменяется, пустой документ РМ получает текст экспертизы."""
        host, port = PG.split(":")
        c = PsqlConn(host, int(port))
        c._run(pg_schema.DROP)
        c._run(pg_schema.COSINE)
        for name in ("001_knowledge_store.sql", "002_form_fields_derived.sql", "003_ai_analysis.sql", "004_env_keys.sql"):
            c._run(pg_schema.shim((pg_schema.MIGRATIONS / name).read_text(encoding="utf-8")))
        s_new, s_rm, s_empty = (hashlib.sha256(x).hexdigest() for x in (b"new", b"rm", b"empty"))
        c._run(f"INSERT INTO ai_documents (content_sha256, text, ocr_status) VALUES ('{s_rm}', 'текст РМ', 'done'), "
               f"('{s_empty}', NULL, 'failed')")
        c._run("INSERT INTO pe_documents (env, expertise_id, filename, sha256, text_full, pages_count) VALUES "
               f"('dev', 1, 'a.PDF', '{s_new}', 'новый', 3), ('dev', 1, 'b.docx', '{s_rm}', 'экспертиза', 1), "
               f"('prod', 2, 'b.docx', '{s_rm}', 'экспертиза', 1), ('dev', 1, 'c.docx', '{s_empty}', 'текст c', 1), "
               "('dev', 1, 'x.docx', 'not-a-sha', 'тест', 1)")
        c._run(pg_schema.shim((pg_schema.MIGRATIONS / "005_shared_documents.sql").read_text(encoding="utf-8")))
        self.assertEqual(c._run(f"SELECT text||'|'||reader||'|'||pages_count||'|'||file_ext FROM ai_documents WHERE content_sha256='{s_new}'"),
                         "новый|file_reader|3|.pdf")
        self.assertEqual(c._run(f"SELECT text FROM ai_documents WHERE content_sha256='{s_rm}'"), "текст РМ")
        self.assertEqual(c._run(f"SELECT text||'|'||ocr_status FROM ai_documents WHERE content_sha256='{s_empty}'"), "текст c|done")
        self.assertEqual(c._run("SELECT count(*) FROM pe_documents WHERE ai_document_id IS NULL"), "1")   # не SHA-256
        self.assertEqual(c._run(f"SELECT count(DISTINCT ai_document_id) FROM pe_documents WHERE sha256='{s_rm}'"), "1")
        pg_schema.build(c)


if __name__ == "__main__":
    unittest.main()
